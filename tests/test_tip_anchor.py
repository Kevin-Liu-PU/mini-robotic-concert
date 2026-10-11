"""Index fingertip 8 supplies beats independently of wrist identity association."""
import math
from types import SimpleNamespace
import unittest

from conductor.multitrack import ConductorSession
from conductor.tracking import Hand, hands_from_result, index_tip_position
from conductor.synthetic import demo_hand


def observation(y=.62, x=.25, wrist_y=.68, label='Right'):
    points=[SimpleNamespace(x=x,y=.55) for _ in range(21)]
    points[0]=SimpleNamespace(x=x,y=wrist_y)
    points[8]=SimpleNamespace(x=x,y=y)
    return Hand(x,wrist_y,label,.99,points)


def drive(s,start,end,source):
    beats=[]
    for i in range(round(start*30),round(end*30)):
        t=i/30
        beat=s.update(source(t),t)
        if beat:
            beats.append(beat)
    return beats


def moving(t,x=.25):
    return observation(.54+.08*math.cos(2*math.pi*t),x)


class TipAnchorTests(unittest.TestCase):
    def test_exact_tip_8_and_wrist_remain_distinct_in_result_adapter(self):
        h=observation(y=.5,wrist_y=.7)
        result=SimpleNamespace(hand_landmarks=[h.landmarks],handedness=[])
        adapted=hands_from_result(result)[0]
        self.assertEqual(adapted.y,.7)
        self.assertAlmostEqual(adapted.index_tip[1],.5)
        self.assertAlmostEqual(adapted.index_tip[0],.25)
        self.assertIsNone(adapted.label)

    def test_other_fingertips_and_base_joints_cannot_replace_tip_8(self):
        h=observation()
        anchor=h.index_tip
        for i in (4,5,9,12,13,16,17,20):
            h.landmarks[i].x=.99
            h.landmarks[i].y=.01
        self.assertEqual(h.index_tip,anchor)

    def test_required_point_invalid_never_falls_back_to_wrist_or_partial_mean(self):
        for index in (8,):
            for invalid in (float('nan'),float('inf'),-.01,1.01,None):
                with self.subTest(index=index,invalid=invalid):
                    h=observation()
                    h.landmarks[index].y=invalid
                    self.assertIsNone(h.index_tip)
        for points in (None,[],[SimpleNamespace(x=.5,y=.5)]*8):
            self.assertIsNone(index_tip_position(points))
        h=observation()
        h.landmarks[8]=None
        self.assertIsNone(h.index_tip)
        self.assertIsNone(Hand(.25,.6,'Right',1).index_tip)

    def test_invalid_tip_cannot_be_clamped_or_replaced_by_valid_other_points(self):
        h=observation(.5)
        h.landmarks[8].y=1.1
        self.assertIsNone(h.index_tip)

    def test_invalid_unused_fingertip_does_not_fabricate_or_move_anchor(self):
        h=observation()
        before=h.index_tip
        h.landmarks[12].x=float('nan')
        self.assertEqual(h.index_tip,before)

    def test_stationary_wrist_with_moving_index_tip_drives_beats_and_index_tip_trail(self):
        s=ConductorSession()
        beats=drive(s,0,12,lambda t:[moving(t)])
        self.assertGreaterEqual(len(beats),11)
        self.assertAlmostEqual(s.engine.bpm,60,delta=.3)
        self.assertEqual(s.tracker.tracks['A'].hand.y,.68)
        self.assertAlmostEqual(s.engine.raw.y,s.hand.index_tip[1])
        self.assertNotAlmostEqual(s.engine.raw.y,s.hand.y,delta=.01)
        self.assertAlmostEqual(s.trajectories['A'].current(11.99).y,s.engine.smoothed.y,delta=.001)

    def test_index_tip_crossing_does_not_change_wrist_identity_assignment(self):
        s=ConductorSession()
        def pair(t):
            a,b=observation(x=.2),observation(x=.8,label='Left')
            for h,target_x in ((a,.5+.05*math.sin(t)),(b,.5-.05*math.sin(t))):
                h.landmarks[8].x=target_x
            return [a,b]
        s.update(pair(0),0)
        tokens={k:v.token for k,v in s.tracker.tracks.items()}
        drive(s,1/30,3,pair)
        self.assertEqual({k:v.token for k,v in s.tracker.tracks.items()},tokens)
        self.assertEqual(s.tracker.last_reason,'tracking')

    def test_invalid_main_index_tip_clears_tempo_without_losing_wrist_identity(self):
        s=ConductorSession()
        drive(s,0,8,lambda t:[moving(t)])
        token=s.primary_token
        self.assertIsNotNone(s.engine.bpm)
        bad=moving(8)
        bad.landmarks[8].y=float('nan')
        self.assertIsNone(s.update([bad],8))
        self.assertEqual(s.primary_token,token)
        self.assertIsNone(s.engine.output_bpm)
        self.assertIsNone(s.engine.previous_beat)
        self.assertEqual(s.engine.count,0)
        self.assertIsNone(s.trajectories['A'].current(8))
        self.assertIn('index tip unavailable',s.display_status())
        for i in range(241,271):
            self.assertIsNone(s.update([observation()],i/30))
        self.assertEqual(len(s.trajectories['A'].segments),2)
        self.assertFalse(any(p.t<8<q.t for p,q in s.trajectories['A'].edges(9,8)))
        beats=drive(s,271/30,18,lambda t:[moving(t)])
        self.assertIsNone(beats[0].interval)
        self.assertAlmostEqual(s.engine.bpm,60,delta=.3)
        self.assertEqual(s.primary_token,token)
        # The pre-loss segment has correctly aged out by 18 seconds.
        self.assertEqual(len(s.trajectories['A'].segments),1)

    def test_invalid_secondary_index_tip_does_not_clear_valid_main_tempo(self):
        s=ConductorSession()
        drive(s,0,1,lambda t:[moving(t)])
        def pair(t):
            bad=observation(x=.75)
            bad.landmarks[8].y=1.1
            return [moving(t),bad]
        drive(s,1,12,pair)
        self.assertEqual(len(s.tracker.tracks),2)
        self.assertEqual(s.primary,'A')
        self.assertAlmostEqual(s.engine.bpm,60,delta=.3)
        self.assertIsNone(s.trajectories['B'].current(11.99))

    def test_demo_geometry_has_requested_index_tip_but_distinct_wrist(self):
        h=demo_hand(.25,.6)
        self.assertAlmostEqual(h.index_tip[0],.25)
        self.assertAlmostEqual(h.index_tip[1],.6)
        self.assertAlmostEqual(h.y,.8)


if __name__=='__main__':
    unittest.main()
