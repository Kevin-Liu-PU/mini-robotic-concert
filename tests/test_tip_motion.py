"""Observed tip/wrist separation and existing continuity limits; no camera."""
import math
import unittest
from conductor.multitrack import ConductorSession, MultiHandTracker
from test_tip_anchor import observation, moving, drive


class TipMotionTests(unittest.TestCase):
    def test_other_points_and_wrist_can_move_without_a_stationary_tip_making_beats(self):
        s=ConductorSession()
        def source(t):
            h=observation(wrist_y=.5+.15*math.cos(2*math.pi*t))
            for i in range(1,21):
                if i!=8:
                    h.landmarks[i].y=h.y
            return [h]
        self.assertEqual(drive(s,0,12,source),[])
        self.assertEqual(s.engine.count,0)

    def test_tip_motion_changes_anchor_without_requiring_other_points_to_move(self):
        h=observation()
        h.landmarks[8].x=.4; h.landmarks[8].y=.45
        self.assertEqual(h.index_tip,(.4,.45))
        self.assertEqual((h.x,h.y),(.25,.68))

    def test_tip_invalid_x_and_missing_point_both_reject_without_fallback(self):
        for value in (float('nan'),float('inf'),-.01,1.01,None):
            h=observation(); h.landmarks[8].x=value
            self.assertIsNone(h.index_tip)

    def test_invalid_unrelated_base_joint_does_not_invalidate_valid_tip(self):
        h=observation()
        h.landmarks[9]=None; h.landmarks[13].y=float('nan')
        self.assertEqual(h.index_tip,(.25,.62))

    def test_label_flips_do_not_filter_or_reset_continuous_main(self):
        s=ConductorSession()
        def source(t):
            h=moving(t)
            h.label='Left' if round(t*30)%2 else 'Right'
            return [h]
        drive(s,0,12,source)
        self.assertAlmostEqual(s.engine.bpm,60,delta=.3)
        self.assertEqual(s.engine.diagnostic_counts['reset:primary_track_segment_ended'],0)
        self.assertEqual(len(s.trajectories['A'].segments),1)

    def test_fast_continuous_translation_inside_guard_retains_main_and_single_beats(self):
        s=ConductorSession()
        positions=(.10,.38,.66,.38)
        events=[]
        for i in range(320):
            t=i/20
            h=observation(.54+.08*math.cos(4*math.pi*t),x=positions[i%4])
            event=s.update([h],t)
            if event:
                events.append(event)
            self.assertIsNone(s.update([h],t))
        self.assertEqual(s.engine.diagnostic_counts['reset:primary_track_segment_ended'],0)
        self.assertEqual(s.engine.diagnostic_counts['reset:position_jump'],0)
        self.assertAlmostEqual(s.engine.bpm,120,delta=1)
        self.assertLessEqual(len(events),32)
        self.assertTrue(all(b.t-a.t>=.28 for a,b in zip(events,events[1:])))

    def test_normal_cadence_guard_boundary_preserves_or_starts_segment(self):
        for distance,continued in ((.299,True),(.301,False)):
            tracker=MultiHandTracker()
            first=tracker.update([observation(x=.1)],0)['A'].token
            next_token=tracker.update([observation(x=.1+distance)],.05)['A'].token
            self.assertEqual(first==next_token,continued)

    def test_elapsed_time_allowance_is_still_bounded_after_dropped_sample(self):
        tracker=MultiHandTracker()
        for i in range(4):
            tracker.update([observation(x=.1+.1*i)],i*.05)
        token=tracker.tracks['A'].token
        tracker.update([observation(x=.98)],.25)
        self.assertEqual(tracker.tracks['A'].token,token)
        tracker.update([observation(x=.2)],.30)
        self.assertNotEqual(tracker.tracks['A'].token,token)

    def test_abrupt_replacement_cannot_inherit_old_tip_beat_or_tempo(self):
        s=ConductorSession()
        drive(s,0,8,lambda t:[moving(t,x=.1)])
        self.assertIsNotNone(s.engine.bpm)
        self.assertIsNone(s.update([observation(y=.8,x=.9)],8))
        self.assertIsNone(s.engine.bpm)
        self.assertIsNone(s.engine.previous_beat)
        self.assertEqual(s.engine.count,0)
        for i in range(241,271):
            self.assertIsNone(s.update([observation(y=.8,x=.9)],i/30))


if __name__=='__main__':
    unittest.main()
