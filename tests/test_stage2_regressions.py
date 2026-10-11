"""Regression traces for the observed processing range; no camera/model needed."""
import math
import unittest
from conductor.core import Conductor
from conductor.tracking import Hand, HandSelector


def trace(engine,fps,start,end,phase=lambda t:2*t,amplitude=.28,drop=None,missing=None):
    events=[]; estimates=[]
    for i in range(round(start*fps),round(end*fps)):
        t=i/fps
        if drop and drop(i):
            continue
        absent=missing and missing(i)
        y=.51+amplitude*math.cos(2*math.pi*phase(t))
        event=engine.update(t,None if absent else .5,None if absent else y)
        if event: events.append(event)
        if engine.bpm is not None: estimates.append((t,engine.bpm))
    return events,estimates


class Stage2Regressions(unittest.TestCase):
    def test_observed_narrow_gate_crossings_survive_smoothing_at_120(self):
        # Raw wrist goes .39..63; smoothing must not erase observed contact.
        for fps in (20,22,24):
            with self.subTest(fps=fps):
                engine=Conductor()
                events,_=trace(engine,fps,0,16,amplitude=.12)
                self.assertGreaterEqual(len(events),31)
                self.assertLessEqual(len(events),32)
                self.assertAlmostEqual(engine.bpm,120,delta=1)

    def test_motion_that_never_reaches_contact_band_is_rejected(self):
        for fps in (20,22,24):
            engine=Conductor()
            trace(engine,fps,0,12,amplitude=.08)
            self.assertEqual(engine.count,0)
            self.assertIsNone(engine.bpm)

    def test_60_to_120_transition_at_real_processing_rates(self):
        for fps in (20,22,24):
            with self.subTest(fps=fps):
                engine=Conductor()
                trace(engine,fps,0,8,phase=lambda t:t)
                self.assertAlmostEqual(engine.bpm,60,delta=1)
                _,estimates=trace(engine,fps,8,16,phase=lambda t:8+2*(t-8))
                self.assertAlmostEqual(engine.bpm,120,delta=1)
                self.assertTrue(any(t<11.5 and abs(bpm-120)<2 for t,bpm in estimates))

    def test_fast_motion_with_dropped_processing_samples(self):
        for fps in (20,22,24):
            with self.subTest(fps=fps):
                engine=Conductor()
                events,_=trace(engine,fps,0,16,drop=lambda i:i%17==0)
                self.assertGreaterEqual(len(events),31)
                self.assertAlmostEqual(engine.bpm,120,delta=1)
                self.assertEqual(engine.diagnostic_counts['reset:position_jump'],0)

    def test_60_to_120_transition_with_dropped_samples(self):
        engine=Conductor()
        phase=lambda t:t if t<8 else 8+2*(t-8)
        _,estimates=trace(engine,20,0,20,phase=phase,drop=lambda i:i%17==0)
        self.assertAlmostEqual(engine.bpm,120,delta=1)
        self.assertTrue(any(t<7 and abs(bpm-60)<1 for t,bpm in estimates))
        self.assertTrue(any(8<t<12 and abs(bpm-120)<2 for t,bpm in estimates))

    def test_brief_loss_suspends_output_and_requires_clean_new_interval(self):
        engine=Conductor()
        trace(engine,20,0,8)
        history=list(engine.intervals)
        engine.update(8,None,None)
        self.assertIsNone(engine.bpm)
        self.assertFalse(engine.armed)
        self.assertEqual(list(engine.intervals),history)
        # Recovery occurs below the beat gate; never emit a recovery-frame beat.
        self.assertIsNone(engine.update(8.05,.5,.51+.28*math.cos(2*math.pi*2*8.05)))
        self.assertIsNone(engine.bpm)
        _,estimates=trace(engine,20,8.10,10)
        self.assertTrue(estimates)
        self.assertAlmostEqual(engine.bpm,120,delta=1)
        self.assertEqual(engine.diagnostic_counts['tracking_recovered:brief_gap'],1)

    def test_missing_downward_crossing_is_never_invented_on_recovery(self):
        engine=Conductor()
        trace(engine,20,0,8.4)
        self.assertTrue(engine.armed)
        count=engine.count
        engine.update(8.4,None,None)
        recovery=engine.update(8.45,.5,.51+.28*math.cos(2*math.pi*2*8.45))
        self.assertIsNone(recovery)
        self.assertEqual(engine.count,count)
        self.assertEqual(engine.diagnostic_counts['crossing_rejected:reacquisition_frame'],1)
        trace(engine,20,8.5,11)
        self.assertAlmostEqual(engine.bpm,120,delta=1)
        self.assertTrue(all(abs(value-.5)<.01 for value in engine.intervals))

    def test_prolonged_loss_destroys_cached_history(self):
        engine=Conductor()
        trace(engine,20,0,8)
        for t in (8,8.05,8.1,8.15,8.2):
            engine.update(t,None,None)
        self.assertEqual(len(engine.intervals),0)
        self.assertIsNone(engine.bpm)
        self.assertIsNone(engine.update(8.25,.5,.8))
        self.assertFalse(engine.armed)

    def test_manual_reset_remains_a_full_reset_and_is_identified(self):
        engine=Conductor()
        trace(engine,20,0,8)
        engine.reset()
        self.assertEqual(engine.count,0)
        self.assertEqual(len(engine.intervals),0)
        self.assertIsNone(engine.bpm)
        self.assertFalse(engine.armed)
        self.assertEqual(engine.diagnostic_counts['reset:manual_reset'],1)
        self.assertEqual(engine.drain_events()[-1]['reason'],'manual_reset')
        engine.reset(reason='camera_change')
        self.assertEqual(engine.diagnostic_counts['reset:camera_change'],1)

    def test_one_invalid_interval_does_not_erase_valid_evidence(self):
        engine=Conductor()
        trace(engine,100,0,8)
        trace(engine,100,8,9,phase=lambda t:16+(t-8)*200/60)
        self.assertGreater(engine.diagnostic_counts['interval_rejected:outside_bpm_range'],0)
        self.assertEqual(len(engine.intervals),7)
        self.assertAlmostEqual(engine.bpm,120,delta=2)

    def test_repeated_invalid_intervals_cannot_keep_old_tempo_alive(self):
        engine=Conductor()
        trace(engine,100,0,8)
        trace(engine,100,8,14,phase=lambda t:16+(t-8)*200/60)
        self.assertIsNone(engine.bpm)
        self.assertGreater(engine.diagnostic_counts['reset:tempo_expired'],0)

    def test_diagnostics_have_reasons_not_landmark_trajectories(self):
        engine=Conductor()
        trace(engine,20,0,6)
        engine.update(6,None,None,missing_reason='locked_label_missing')
        events=engine.drain_events()
        self.assertTrue(any(e['reason']=='locked_label_missing' for e in events))
        self.assertTrue(any(e['event']=='beat' and 'interval_s' in e for e in events))
        self.assertFalse(any(set(e)&{'x','y','landmarks','image','frame'} for e in events))

    def test_hand_selector_uses_elapsed_time_for_dropped_samples(self):
        selector=HandSelector()
        for i in range(320):
            if i%17==0: continue
            t=i/20
            hand=Hand(.5,.51+.28*math.cos(2*math.pi*2*t),'Left',.99)
            self.assertIs(selector.choose([hand],t),hand)


if __name__=='__main__':
    unittest.main()
