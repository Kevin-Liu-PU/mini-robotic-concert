"""A supported MAIN must survive an irrelevant candidate, without identity swaps."""
import math
import json
import random
import unittest

from conductor.multitrack import ConductorSession
from conductor.core import DetectorConfig
from conductor.tracking import AssociationConfig
from tests.test_tip_anchor import drive, moving, observation


class MainContinuityTests(unittest.TestCase):
    def stable(self):
        session = ConductorSession()
        drive(session, 0, 8, lambda t: [moving(t)])
        self.assertAlmostEqual(session.engine.bpm, 60, delta=.3)
        return session

    def test_unchanged_main_plus_one_frame_near_candidate_preserves_evidence(self):
        session = self.stable()
        token = session.primary_token
        intervals = list(session.engine.intervals)
        main = moving(8)
        session.update([main, observation(x=.30)], 8)
        self.assertEqual(session.primary_token, token)
        self.assertIs(session.hand, main)
        self.assertAlmostEqual(session.engine.bpm, 60, delta=.3)
        self.assertGreaterEqual(len(session.engine.intervals), len(intervals))
        self.assertEqual(len(session.tracker.tracks), 1)
        self.assertEqual(len(session.trajectories['A'].segments), 1)
        self.assertIsNone(session.engine.held_tempo)
        self.assertEqual(session.engine.diagnostic_counts['reset:primary_track_segment_ended'], 0)

    def test_tempo_and_beat_times_equal_baseline_with_recurring_candidate_and_order_swaps(self):
        for bpm in (60, 120):
            baseline, challenged = ConductorSession(), ConductorSession()
            beats_a, beats_b = [], []
            for i in range(480):
                t = i/30
                y = .54+.08*math.cos(2*math.pi*t*bpm/60)
                a, b = observation(y), observation(y)
                candidates = [b]
                if i > 30 and i % 4 != 0:
                    candidates.append(observation(.8, x=.30))
                if i % 2:
                    candidates.reverse()
                ea, eb = baseline.update([a], t), challenged.update(candidates, t)
                if ea:
                    beats_a.append(ea.t)
                if eb:
                    beats_b.append(eb.t)
            self.assertEqual(beats_a, beats_b)
            self.assertAlmostEqual(challenged.engine.bpm, bpm, delta=.3)
            self.assertEqual(challenged.engine.diagnostic_counts['reset:primary_track_segment_ended'], 0)
            self.assertEqual(len(challenged.trajectories['A'].segments), 1)

    def test_clear_separation_with_small_measured_wrist_motion_preserves_main(self):
        s = self.stable()
        token = s.primary_token
        for i in range(240, 300):
            t = i/30
            x = .25+.004*math.sin(i)
            main = observation(.54+.08*math.cos(2*math.pi*t), x=x)
            s.update([observation(.8, x=x+.07), main], t)
            self.assertEqual(s.primary_token, token)
            self.assertIs(s.hand, main)
        self.assertAlmostEqual(s.engine.bpm, 60, delta=.3)

    def assert_lost_without_tempo(self, s):
        self.assertIsNone(s.primary)
        self.assertIsNone(s.engine.output_bpm)
        self.assertIsNone(s.engine.previous_beat)
        self.assertFalse(s.engine.intervals)

    def test_exact_and_near_identical_duplicates_are_not_unique_identity_evidence(self):
        for offset in (0, .001, .01, .019):
            s = self.stable()
            self.assertIsNone(s.update([moving(8), observation(x=.25+offset)], 8))
            self.assert_lost_without_tempo(s)
            self.assertEqual(s.tracker.last_reason, 'ambiguous_close_wrists')

    def test_equidistant_candidates_stop_even_when_one_is_labelled_like_main(self):
        s = self.stable()
        s.update([observation(x=.225,label='Right'), observation(x=.275,label='Left')], 8)
        self.assert_lost_without_tempo(s)

    def test_initial_close_pair_cannot_claim_main(self):
        s = ConductorSession()
        drive(s, 0, 1, lambda t: [observation(x=.25),observation(x=.30)])
        self.assert_lost_without_tempo(s)

    def test_two_established_tracks_still_stop_when_they_approach(self):
        s = self.stable()
        drive(s, 8, 9, lambda t: [observation(x=.25), observation(x=.75)])
        for i in range(15):
            t = 9+i/30
            s.update([observation(x=.25+.015*i),observation(x=.75-.015*i)],t)
            if s.primary is None:
                break
        self.assert_lost_without_tempo(s)
        self.assertEqual(s.tracker.last_reason, 'ambiguous_close_wrists')

    def test_merged_observation_of_two_established_tracks_stops(self):
        s = self.stable()
        drive(s, 8, 9, lambda t: [observation(x=.25),observation(x=.65)])
        s.update([observation(x=.45)], 9)
        self.assert_lost_without_tempo(s)
        self.assertEqual(s.tracker.last_reason, 'ambiguous_assignment')

    def test_suppressed_candidate_cannot_inherit_main_when_main_disappears(self):
        s = self.stable()
        old = s.primary_token
        s.update([moving(8),observation(x=.30)], 8)
        self.assertEqual(s.primary_token,old)
        self.assertIsNone(s.update([observation(x=.30,y=.8)],8+1/30))
        self.assert_lost_without_tempo(s)
        self.assertEqual(s.tracker.last_reason,'ambiguous_recent_competitor')
        drive(s, 8+2/30, 9, lambda t: [observation(x=.30,y=.8)])
        self.assertNotEqual(s.primary_token,old)
        self.assertEqual(s.engine.count,0)
        self.assertFalse(any(a.t<=8<b.t for a,b in s.trajectories['A'].edges(9,8)))

    def test_remembered_candidate_crossing_main_is_not_silently_ignored(self):
        s = self.stable()
        s.update([moving(8),observation(x=.30)],8)
        s.update([moving(8+1/30),observation(x=.20)],8+1/30)
        self.assert_lost_without_tempo(s)
        self.assertEqual(s.tracker.last_decision['continuity_check'],'remembered_competitor_conflict')

    def test_single_merged_midpoint_after_suppression_is_ambiguous(self):
        s = self.stable()
        s.update([moving(8),observation(x=.30)],8)
        s.update([observation(x=.275)],8+1/30)
        self.assert_lost_without_tempo(s)

    def test_competitor_memory_is_bounded_and_does_not_draw_ghost_points(self):
        s = self.stable()
        old = s.primary_token
        s.update([moving(8),observation(x=.30)],8)
        drive(s, 8+1/30, 9, lambda t: [moving(t)])
        self.assertIsNone(s.tracker.competitor)
        self.assertEqual(s.primary_token,old)
        self.assertFalse(s.trajectories['B'].segments)
        self.assertAlmostEqual(s.engine.bpm,60,delta=.3)

    def test_one_empty_frame_still_breaks_identity_and_all_intervals(self):
        s = self.stable()
        s.update([moving(8),observation(x=.30)],8)
        self.assertIsNone(s.update([],8+1/30))
        self.assert_lost_without_tempo(s)
        self.assertIsNone(s.tracker.competitor)
        drive(s,8+2/30,9,lambda t:[moving(t)])
        self.assertFalse(any(a.t<8+1/30<b.t for a,b in s.trajectories['A'].edges(9,8)))

    def test_long_unobserved_gap_cannot_use_old_continuity_certificate(self):
        s = self.stable()
        s.update([moving(20),observation(x=.30)],20)
        self.assert_lost_without_tempo(s)
        self.assertEqual(s.tracker.last_decision['prior_slots'],[])

    def test_certificate_cannot_bypass_a_stricter_configured_displacement_guard(self):
        s=ConductorSession(DetectorConfig(max_position_jump=.01))
        drive(s,0,1,lambda t:[observation(.55)])
        self.assertIsNotNone(s.primary)
        s.update([observation(.55,x=.265),observation(.55,x=.315)],1)
        self.assert_lost_without_tempo(s)
        self.assertAlmostEqual(s.tracker.last_decision['continuity_limit'],.01)

    def test_far_new_hand_remains_independent_and_does_not_change_main(self):
        s = self.stable()
        old = s.primary_token
        s.update([observation(x=.85),moving(8)],8)
        self.assertEqual(s.primary_token,old)
        self.assertEqual(len(s.tracker.tracks),2)
        self.assertAlmostEqual(s.engine.bpm,60,delta=.3)

    def test_fast_single_hand_uses_existing_motion_guard_without_exception(self):
        s = self.stable()
        old = s.primary_token
        for i in range(240,300):
            x = (.1,.38,.66,.38)[i%4]
            s.update([observation(.54+.08*math.cos(2*math.pi*i/30),x=x)],i/30)
            self.assertEqual(s.primary_token,old)
        self.assertAlmostEqual(s.engine.bpm,60,delta=.3)

    def test_fast_current_ambiguous_motion_still_stops(self):
        s = self.stable()
        s.update([observation(x=.31),observation(x=.36)],8)
        self.assert_lost_without_tempo(s)

    def test_accepted_fast_history_does_not_veto_clear_current_step(self):
        s = self.stable()
        token = s.primary_token
        s.update([observation(x=.31)],8)
        s.update([observation(x=.31),observation(x=.36)],8+1/30)
        self.assertEqual(s.primary_token,token)
        self.assertEqual(s.tracker.last_decision['continuity_check'],'supported_observed_main')
        self.assertGreater(s.tracker.last_decision['continuity_recent_max_step'],.03)

    def test_invalid_main_tip_never_uses_other_candidates_tip(self):
        s = self.stable()
        old = s.primary_token
        main = moving(8)
        main.landmarks[8].y=float('nan')
        self.assertIsNone(s.update([main,observation(x=.30)],8))
        self.assertEqual(s.primary_token,old)
        self.assertIsNone(s.engine.output_bpm)
        self.assertIsNone(s.trajectories['A'].current(8))

    def test_primary_b_is_supported_without_renaming_it_a(self):
        s = ConductorSession()
        s.update([observation(x=.1),observation(x=.7)],0)
        s.select_primary('B')
        drive(s,1/30,1,lambda t:[observation(x=.7)])
        old = s.primary_token
        s.update([observation(x=.75),observation(x=.7)],1)
        self.assertEqual(s.primary_token,old)
        self.assertEqual(s.primary,'B')

    def test_duplicate_timestamp_does_not_change_memory_or_count(self):
        s = self.stable()
        s.update([moving(8),observation(x=.30)],8)
        old = s.primary_token,s.tracker.competitor,s.engine.count
        for t in (8,7.9,float('nan')):
            self.assertIsNone(s.update([observation(x=.9)],t))
        self.assertEqual((s.primary_token,s.tracker.competitor,s.engine.count),old)

    def test_decisions_explain_exception_and_normal_cost_ambiguity_without_coordinates(self):
        s = self.stable()
        s.update([moving(8),observation(x=.30)],8)
        d = s.tracker.last_decision
        self.assertAlmostEqual(d['best_assignment_cost'],0)
        self.assertAlmostEqual(d['second_assignment_cost'],.05)
        self.assertEqual(d['continuity_check'],'supported_observed_main')
        self.assertEqual(d['suppressed_candidate_indices'],[1])
        self.assertEqual(d['assigned_candidates'],{'A':0})
        self.assertEqual(d['prior_slots'],['A'])
        self.assertEqual(d['wrist_distances'],[[0,.04999999999999999]])
        self.assertFalse(set(d)&{'x','y','landmarks','image','frames'})
        json.dumps(d,allow_nan=False)

    def test_continuity_settings_validate_finite_positive_distances_and_ratio(self):
        for kw in ({'continuity_ratio':1},{'continuity_ratio':float('nan')},
                   {'continuity_radius':1.1},{'continuity_separation':0}):
            with self.assertRaises(ValueError):
                AssociationConfig(**kw)

    def test_tempo_change_jitter_dropped_processing_samples_and_duplicates_match_baseline(self):
        a,b = ConductorSession(),ConductorSession()
        rng = random.Random(74)
        t=0
        events_a,events_b=[],[]
        while t<20:
            phase=t if t<8 else 8+(t-8)*1.5
            y=.54+.08*math.cos(2*math.pi*phase)+rng.uniform(-.002,.002)
            x=.25+rng.uniform(-.002,.002)
            main_a,main_b=observation(y,x),observation(y,x)
            candidates=[main_b]
            if t>1 and rng.random()<.8:
                candidates.append(observation(.8,x+.06))
            rng.shuffle(candidates)
            ea,eb=a.update([main_a],t),b.update(candidates,t)
            if ea:
                events_a.append(ea.t)
            if eb:
                events_b.append(eb.t)
            self.assertIsNone(b.update(candidates,t))
            t+=(1+rng.uniform(-.1,.1))/24*(2 if rng.random()<.06 else 1)
        self.assertEqual(events_a,events_b)
        self.assertAlmostEqual(b.engine.bpm,90,delta=1.5)
        self.assertEqual(b.engine.diagnostic_counts['reset:primary_track_segment_ended'],0)


if __name__ == '__main__':
    unittest.main()
