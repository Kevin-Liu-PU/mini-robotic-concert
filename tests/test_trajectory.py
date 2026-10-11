"""Display-history safety tests; no camera, model, NumPy or OpenCV needed."""
import math
import unittest

from conductor.core import Conductor
from conductor.tracking import Hand,HandSelector
from conductor.trajectory import Trajectory,trail_opacity


def seed():
    trail=Trajectory()
    trail.update(0,.2,.3)
    trail.update(.05,.25,.35)
    trail.update(.1,.3,.4)
    return trail


class TrajectoryTests(unittest.TestCase):
    def test_missing_sample_keeps_old_segment_but_hides_current_marker(self):
        trail=seed();before=list(trail.edges(.1,3))
        trail.update(.15,None,None)
        self.assertEqual(list(trail.edges(.15,3)),before)
        self.assertIsNone(trail.current(.15))

    def test_recovery_starts_new_segment_without_bridge_or_old_smoothing(self):
        trail=seed();trail.update(.15,None,None)
        point=trail.update(.2,.8,.7)
        self.assertEqual((point.x,point.y),(.8,.7))
        trail.update(.25,.82,.72)
        edges=list(trail.edges(.25,3))
        self.assertEqual(len(trail.segments),2)
        self.assertEqual(len(edges),3)
        self.assertFalse(any(p.t<.15<q.t for p,q in edges))

    def test_nonfinite_and_out_of_frame_samples_break_without_erasing(self):
        for x,y in ((.5,float('nan')),(float('inf'),.5),(.5,1.01),(-.01,.5)):
            with self.subTest(x=x,y=y):
                trail=seed()
                self.assertIsNone(trail.update(.15,x,y))
                self.assertEqual(len(list(trail.edges(.15,3))),2)
                trail.update(.2,.4,.4)
                self.assertEqual(len(trail.segments),2)

    def test_rejected_detection_creates_gap_while_retaining_prior_history(self):
        selector=HandSelector();trail=Trajectory()
        for t in (0,.05,.1):
            hand=selector.choose([Hand(.5,.5,'Right',.9)],t)
            trail.update(t,hand.x,hand.y)
        hand=selector.choose([Hand(.5,1.01,'Left',.99)],.15)
        self.assertIsNone(hand)
        trail.update(.15,None,None)
        hand=selector.choose([Hand(.51,.51,'Right',.9)],.2)
        trail.update(.2,hand.x,hand.y)
        self.assertEqual(len(list(trail.edges(.2,3))),2)
        self.assertEqual(len(trail.segments),2)

    def test_long_frame_gap_and_position_jump_break_segments(self):
        for t,x,y in ((.5,.31,.41),(.15,.95,.95)):
            trail=seed()
            trail.update(t,x,y)
            self.assertEqual(len(trail.segments),2)
            self.assertEqual(len(list(trail.edges(t,3))),2)

    def test_three_second_visible_lifetime_is_separate_from_eight_second_graph(self):
        trail=seed();trail.update(.15,None,None)
        self.assertEqual(len(list(trail.edges(2.99,3))),2)
        self.assertEqual(list(trail.edges(3.11,3)),[])
        self.assertEqual(len(list(trail.edges(3.11,8))),2)
        trail.prune(8.11)
        self.assertEqual(len(trail.segments),0)

    def test_fade_reaches_zero_at_original_lifetime(self):
        self.assertEqual(trail_opacity(.1,.1,3),1)
        self.assertAlmostEqual(trail_opacity(1.6,.1,3),.5)
        self.assertAlmostEqual(trail_opacity(3.1,.1,3),0)
        self.assertEqual(trail_opacity(4,.1,3),0)

    def test_r_clear_removes_all_segments_and_smoothing(self):
        trail=seed();trail.update(.15,None,None);trail.update(.2,.8,.7)
        trail.clear()
        self.assertEqual(len(trail.segments),0)
        self.assertIsNone(trail.current(.2))
        point=trail.update(.25,.1,.2)
        self.assertEqual((point.x,point.y),(.1,.2))
        self.assertEqual(list(trail.edges(.25,3)),[])

    def test_tempo_expiry_does_not_erase_or_restart_display_history(self):
        engine=Conductor();trail=Trajectory()
        for i in range(240):
            t=i/20
            y=.51+.28*math.cos(2*math.pi*t) if t<8 else .79
            engine.update(t,.5,y)
            trail.update(t,.5,y)
            engine.expire(t)
        self.assertGreater(engine.diagnostic_counts['reset:tempo_expired'],0)
        self.assertIsNone(engine.bpm)
        self.assertEqual(len(trail.segments),1)
        self.assertGreater(len(list(trail.edges(11.95,3))),50)
        self.assertIsNotNone(trail.current(11.95))

    def test_idle_current_marker_expires_but_history_does_not_disappear_early(self):
        trail=seed()
        self.assertIsNone(trail.current(.5))
        self.assertEqual(len(list(trail.edges(.5,3))),2)

    def test_duplicate_timestamp_does_not_add_or_connect_points(self):
        trail=seed();before=list(trail.edges(.1,3))
        self.assertIsNone(trail.update(.1,.9,.9))
        self.assertIsNone(trail.update(.09,None,None))
        self.assertEqual(list(trail.edges(.1,3)),before)

    def test_height_graph_also_has_no_bridge_across_brief_loss(self):
        trail=seed();trail.update(.15,None,None)
        trail.update(.2,.8,.7);trail.update(.25,.82,.72)
        edges=list(trail.edges(5,8))
        self.assertEqual(len(edges),3)
        self.assertFalse(any(p.t<.15<q.t for p,q in edges))

    def test_explicit_tracking_interruption_starts_new_segment_without_erasure(self):
        trail=seed()
        trail.break_segment()
        trail.update(.15,.31,.41)
        self.assertEqual(len(trail.segments),2)
        self.assertEqual(len(list(trail.edges(.15,3))),2)


if __name__=='__main__':
    unittest.main()
