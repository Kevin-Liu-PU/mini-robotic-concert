"""Synthetic two-hand velocity/position tests; standard library, no devices."""
import copy
import math
import unittest

from conductor.multitrack import ConductorSession
from conductor.settings import load_config
from conductor.core import DetectorConfig
from conductor.virtual_bow import HandObservation as H, VirtualBow, VirtualBowConfig, demo_hands, update_from_session


def pair(t, y, x=.25, *, by=None, bx=None, generation=1):
    return {'A': H(('A', generation), t, x, y),
            'B': H(('B', generation), t, 1-x if bx is None else bx, y if by is None else by)}


class VirtualBowTests(unittest.TestCase):
    def moving(self, **config):
        bow = VirtualBow(VirtualBowConfig(**config))
        bow.observe(pair(0, .6), 0)
        bow.observe(pair(.1, .59), .1)
        self.assertEqual(bow.direction, 1)
        return bow

    def test_slow_up_fast_down_immediately_reverses_and_accelerates(self):
        bow = self.moving()
        slow = bow.speed
        bow.observe(pair(.2, .63), .2)
        self.assertEqual(bow.direction, -1)
        self.assertAlmostEqual(bow.speed, slow*4)
        self.assertEqual(bow.reversals, 1)
        before = bow.position
        bow.advance(.25)
        self.assertLess(bow.position, before)

    def test_mirrored_horizontal_motion_does_not_cancel_speed(self):
        bow = VirtualBow()
        bow.observe(pair(0, .6), 0)
        bow.observe(pair(.1, .59, x=.27), .1)
        self.assertAlmostEqual(bow.measured['A']['vx'], .2)
        self.assertAlmostEqual(bow.measured['B']['vx'], -.2)
        self.assertAlmostEqual(bow.speed, .6*math.hypot(.2, .1))

    def test_direction_calibration_inverts_only_direction(self):
        normal = self.moving()
        inverted = self.moving_for_inverted()
        self.assertEqual(inverted.direction, -normal.direction)
        self.assertEqual(normal.speed, inverted.speed)

    def moving_for_inverted(self):
        bow = VirtualBow(VirtualBowConfig(invert_direction=True))
        bow.observe(pair(0, .6), 0); bow.observe(pair(.1, .59), .1)
        return bow

    def test_disagreement_stationary_and_horizontal_legs_stop(self):
        cases = [(pair(.2, .58, by=.60), 'DIRECTION_DISAGREEMENT'),
                 (pair(.2, .58, by=.59), 'VERTICAL_DEADBAND'),
                 (pair(.2, .59, x=.27), 'VERTICAL_DEADBAND'),
                 (pair(.2, .58, by=.49), 'SPEED_DISAGREEMENT')]
        for hands, reason in cases:
            with self.subTest(reason=reason):
                bow = self.moving(); bow.observe(hands, .2)
                self.assertEqual((bow.state, bow.reason, bow.speed), ('STOPPED', reason, 0))

    def test_missing_hand_stops_without_catchup_then_needs_two_fresh_samples(self):
        bow = self.moving(); position = bow.position
        bow.observe({'A': pair(.2, .58)['A']}, .2)
        self.assertEqual(bow.speed, 0); self.assertEqual(bow.position, position)
        bow.observe(pair(.3, .57), .3); self.assertEqual(bow.speed, 0)
        bow.observe(pair(.4, .56), .4); self.assertGreater(bow.speed, 0)

    def test_invalid_coordinates_future_stale_and_duplicate_observations_stop(self):
        for t, y, now in ((.2, math.nan, .2), (.2, 1.01, .2), (.3, .58, .2),
                          (.2, .58, .5), (.1, .58, .2), (.05, .58, .2)):
            with self.subTest(t=t, y=y, now=now):
                bow = self.moving(); position = bow.position
                bow.observe(pair(t, y), now)
                self.assertEqual(bow.speed, 0); self.assertEqual(bow.position, position)

    def test_unsynchronized_hands_are_not_combined(self):
        bow = self.moving(); hands = pair(.2, .58)
        hands['B'] = H(('B', 1), .19, .75, .58)
        bow.observe(hands, .2)
        self.assertEqual(bow.reason, 'UNSYNCHRONIZED_OR_AMBIGUOUS_HANDS')

    def test_new_track_generation_stops_and_never_bridges_velocity(self):
        bow = self.moving(); position = bow.position
        bow.observe(pair(.2, .4, generation=2), .2)
        self.assertEqual(bow.speed, 0); self.assertEqual(bow.position, position)
        bow.observe(pair(.3, .39, generation=2), .3)
        self.assertAlmostEqual(bow.speed, .06)

    def test_stale_ui_poll_stops_without_extrapolating_or_teleporting(self):
        bow = self.moving(); position = bow.position
        bow.advance(.5)
        self.assertEqual(bow.position, position); self.assertEqual(bow.speed, 0)
        bow.observe(pair(.51, .4), .51); self.assertEqual(bow.speed, 0)
        bow.observe(pair(.61, .39), .61); self.assertGreater(bow.speed, 0)

    def test_gap_between_observations_cannot_be_hidden_by_frequent_ui_polls(self):
        bow = self.moving()
        for t in (.15, .2, .25, .3): bow.advance(t)
        bow.observe(pair(.4, .4), .4)
        self.assertEqual(bow.speed, 0)

    def test_speed_outlier_is_rejected_and_valid_speed_is_bounded(self):
        bow = self.moving()
        bow.observe(pair(.11, .4), .11)
        self.assertEqual(bow.reason, 'IMPLAUSIBLE_HAND_SPEED')
        bow = self.moving(speed_gain=100, max_speed=.2)
        self.assertEqual(bow.speed, .2)

    def test_bounds_stop_outward_motion_and_allow_fresh_inward_reversal(self):
        bow = VirtualBow(VirtualBowConfig(speed_gain=5))
        for i in range(21): bow.observe(pair(i*.1, .8-i*.02), i*.1)
        self.assertEqual((bow.position, bow.speed, bow.reason), (1, 0, 'TRAVEL_LIMIT'))
        bow.observe(pair(2.1, .43), 2.1)
        self.assertEqual(bow.direction, -1)
        bow.observe(pair(2.2, .46), 2.2)
        self.assertLess(bow.position, 1)
        bow.position = .01
        bow.advance(2.3)
        self.assertEqual((bow.position, bow.speed), (0, 0))

    def test_manual_pause_reset_and_invalid_clock_never_auto_resume(self):
        for action in ('pause', 'reset', 'clock'):
            bow = self.moving()
            if action == 'pause': bow.key(32)
            elif action == 'reset': bow.key(ord('r')); self.assertEqual(bow.position, .5)
            else: bow.advance(float('nan'))
            bow.observe(pair(.2, .58), .2)
            self.assertFalse(bow.enabled); self.assertEqual(bow.speed, 0)
            bow.key(13)
            bow.observe(pair(.3, .57), .3); self.assertEqual(bow.speed, 0)
            bow.observe(pair(.4, .56), .4); self.assertGreater(bow.speed, 0)

    def test_magnitude_smoothing_is_bounded_and_does_not_change_direction(self):
        bow = self.moving()
        bow.observe(pair(.12, .582), .12)
        self.assertGreater(bow.speed, .06); self.assertLess(bow.speed, .24)
        self.assertEqual(bow.direction, 1)

    def test_configuration_rejects_invalid_or_unbounded_settings(self):
        for config in ({'max_speed': 2}, {'max_age_s': 1}, {'max_gap_s': 1},
                       {'speed_gain': 0}, {'smoothing_tau_s': math.nan},
                       {'invert_direction': 1}, {'max_speed_ratio': .5}):
            with self.subTest(config=config), self.assertRaises(ValueError):
                VirtualBowConfig(**config)

    def test_real_tracker_seam_reuses_mirrored_trajectories_without_mutation(self):
        session = ConductorSession(DetectorConfig(**load_config()['detector']))
        bow = VirtualBow(); directions = set(); speeds = {}
        for i in range(301):
            t = i/60
            session.update(demo_hands(t), t)
            before = copy.deepcopy(session.details()), session.engine.reset_generation, session.engine.count
            update_from_session(bow, session, True, t)
            self.assertEqual(before, (session.details(), session.engine.reset_generation, session.engine.count))
            directions.add(bow.direction)
            if i in (120, 210, 270): speeds[i] = bow.speed
        self.assertEqual(directions, {-1, 0, 1})
        self.assertGreater(speeds[210], speeds[120])
        self.assertEqual(speeds[270], 0)
        self.assertTrue(all(session.trajectories[s].segments for s in ('A', 'B')))


if __name__ == '__main__':
    unittest.main()
