"""Hand-lock, freshness and coordinate audit without images or a camera."""
import math
from types import SimpleNamespace
import unittest

from conductor.core import Conductor
from conductor.tracking import Hand,HandSelector,select_fresh_result


def result(label='Right',x=.5,y=.5):
    return SimpleNamespace(hand_landmarks=[[SimpleNamespace(x=x,y=y)]],
                           handedness=[[SimpleNamespace(category_name=label,score=.99)]])


class TrackingAuditTests(unittest.TestCase):
    def test_stale_first_result_cannot_acquire_wrong_label_lock(self):
        selector=HandSelector()
        self.assertIsNone(select_fresh_result(selector,result('Left'),10,10.3,.25))
        self.assertIsNone(selector.label)
        self.assertIsNone(selector.previous)
        self.assertEqual(selector.last_reason,'inference_result_too_old')
        self.assertIsNotNone(select_fresh_result(selector,result('Right'),10.35,10.37,.25))
        self.assertEqual(selector.label,'Right')

    def test_stale_result_cannot_move_existing_motion_anchor(self):
        selector=HandSelector()
        select_fresh_result(selector,result(),10,10.02,.25)
        before=(selector.label,selector.previous,selector.previous_t,list(selector.sample_dts))
        self.assertIsNone(select_fresh_result(selector,result(x=.7),10.05,10.4,.25))
        self.assertEqual(before,(selector.label,selector.previous,selector.previous_t,list(selector.sample_dts)))

    def test_core_watermark_is_checked_before_hand_selection(self):
        selector=HandSelector()
        for timestamp in (10,10.1):
            self.assertIsNone(select_fresh_result(selector,result('Left'),timestamp,10.15,.25,10.1))
            self.assertIsNone(selector.label)
        self.assertIsNotNone(select_fresh_result(selector,result('Right'),10.12,10.15,.25,10.1))
        self.assertEqual(selector.label,'Right')

    def test_invalid_or_future_timestamp_cannot_lock_hand(self):
        for t in (float('nan'),float('inf'),11):
            selector=HandSelector()
            self.assertIsNone(select_fresh_result(selector,result(),t,10,.25))
            self.assertIsNone(selector.label)

    def test_any_tracks_nearby_wrist_despite_label_change(self):
        selector=HandSelector('any')
        self.assertIsNotNone(selector.choose([Hand(.5,.5,'Right',.99)],1))
        self.assertIsNotNone(selector.choose([Hand(.5,.5,'Left',.99)],1.05))
        self.assertEqual(selector.last_details['detected_hands'],1)
        self.assertEqual(selector.last_details['left_label_hands'],1)
        self.assertTrue(selector.last_details['model_label_changed'])
        self.assertEqual(selector.display_status(),'Spatial track; model label Left')
        selector.reset()
        self.assertIsNotNone(selector.choose([Hand(.5,.5,'Left',.99)],1.1))

    def test_explicit_right_is_only_initial_preference_not_identity(self):
        selector=HandSelector('Right')
        self.assertIsNotNone(selector.choose([Hand(.5,.5,'Left',.99)],1))
        self.assertEqual(selector.last_details['valid_wrists'],1)
        self.assertEqual(selector.last_details['detected_hands'],1)
        self.assertEqual(selector.label,'Left')

    def test_mirroring_x_and_static_label_mapping_preserve_beat_times(self):
        originals=Conductor();mirrored=Conductor()
        first=HandSelector();second=HandSelector()
        beat_a=[];beat_b=[]
        for i in range(240):
            t=i/20;x=.45+.05*math.sin(t);y=.51+.28*math.cos(2*math.pi*t)
            a=first.choose([Hand(x,y,'Right',.99)],t)
            b=second.choose([Hand(1-x,y,'Left',.99)],t)
            self.assertIsNotNone(a);self.assertIsNotNone(b)
            e1=originals.update(t,a.x,a.y);e2=mirrored.update(t,b.x,b.y)
            if e1:beat_a.append(e1.t)
            if e2:beat_b.append(e2.t)
        self.assertEqual(beat_a,beat_b)
        self.assertAlmostEqual(originals.bpm,60,delta=.2)
        self.assertEqual(originals.bpm,mirrored.bpm)

    def test_invalid_wrist_is_filtered_without_switch_or_fabricated_beat(self):
        selector=HandSelector();engine=Conductor()
        for i in range(160):
            t=i/20;y=.51+.28*math.cos(2*math.pi*t)
            hand=selector.choose([Hand(.5,y,'Right',.99)],t)
            engine.update(t,hand.x,hand.y)
        history=list(engine.intervals);count=engine.count
        hand=selector.choose([Hand(.5,1.01,'Left',.99)],8)
        self.assertIsNone(hand)
        engine.update(8,None,None,missing_reason=selector.last_reason)
        self.assertEqual(selector.label,'Right')
        self.assertEqual(list(engine.intervals),history)
        self.assertIsNone(engine.bpm)
        self.assertIsNotNone(engine.output_bpm)
        hand=selector.choose([Hand(.5,.51+.28*math.cos(2*math.pi*8.05),'Right',.99)],8.05)
        self.assertIsNone(engine.update(8.05,hand.x,hand.y))
        self.assertEqual(engine.count,count)
        self.assertEqual(list(engine.intervals),history)
        self.assertIsNone(engine.previous_beat)

    def test_ui_distinguishes_no_detection_from_filtering(self):
        selector=HandSelector()
        selector.choose([],1)
        self.assertIn('No hand detected',selector.display_status())
        selector.choose([Hand(.5,.5,None,0)],1.05)
        self.assertEqual(selector.display_status(),'Spatial track; model label unknown')


if __name__=='__main__':
    unittest.main()
