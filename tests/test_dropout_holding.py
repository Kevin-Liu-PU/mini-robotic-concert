"""Offline regressions for the instrumented trial; no camera or MediaPipe."""
import math
from types import SimpleNamespace
import unittest

from conductor.core import Conductor, DetectorConfig, TempoMetronome
from conductor.tracking import Hand, HandSelector, hands_from_result


def y_at(t,bpm=73.35230675152151):
    return .51+.28*math.cos(2*math.pi*t*bpm/60)


def advance(engine,start,end,bpm=73.35230675152151,fps=20):
    events=[]
    for i in range(round(start*fps),round(end*fps)):
        t=i/fps
        event=engine.update(t,.5,y_at(t,bpm))
        if event:
            events.append(event)
    return events


class DropoutHoldingTests(unittest.TestCase):
    def primed(self):
        engine=Conductor()
        advance(engine,0,8)
        self.assertAlmostEqual(engine.bpm,73.35,delta=.2)
        return engine

    def test_single_invalid_frame_at_73_bpm_holds_instead_of_clearing(self):
        for invalid in (float('nan'),float('inf'),-.01,1.01):
            with self.subTest(invalid=invalid):
                engine=Conductor()
                # The live invalid sample followed the published beat by .128 s.
                # Inject just after a beat, rather than across the next crossing.
                advance(engine,0,7.4)
                prior=engine.bpm; count=engine.count; history=list(engine.intervals)
                self.assertAlmostEqual(prior,73.35,delta=.2)
                self.assertIsNone(engine.update(7.4,.5,invalid))
                self.assertIsNone(engine.bpm)
                self.assertEqual(engine.output_bpm,prior)
                self.assertEqual(engine.tempo_state,'HOLDING / STALE')
                self.assertIsNone(engine.raw)
                self.assertIsNone(engine.smoothed)
                self.assertFalse(engine.armed)
                self.assertIsNone(engine.update(7.45,.5,y_at(7.45)))
                self.assertEqual(engine.count,count)
                self.assertEqual(list(engine.intervals),history)
                self.assertEqual(engine.output_bpm,prior)
                advance(engine,7.5,9)
                self.assertAlmostEqual(engine.bpm,73.35,delta=.2)
                self.assertEqual(engine.tempo_state,'MEASURED')
                self.assertTrue(all(abs(v-60/73.3523)<.01 for v in engine.intervals))
                self.assertEqual(engine.diagnostic_counts['reset:invalid_landmark'],0)

    def test_sustained_invalid_tracking_clears_hold_and_history(self):
        engine=self.primed()
        for t in (8,8.05,8.1):
            engine.update(t,.5,1.01)
            self.assertIsNotNone(engine.output_bpm)
        engine.expire(8.121)
        self.assertIsNone(engine.output_bpm)
        self.assertEqual(len(engine.intervals),0)
        self.assertIsNone(engine.update(8.15,.5,.8))
        self.assertFalse(engine.armed)

    def test_repeated_brief_gaps_cannot_extend_holding_deadline(self):
        engine=Conductor(DetectorConfig(tempo_hold_s=.4))
        advance(engine,0,8)
        engine.update(8,None,None)
        for i in range(161,169):
            t=i/20
            engine.update(t,None if i%4==0 else .5,None if i%4==0 else y_at(7.95))
        engine.expire(8.401)
        self.assertIsNone(engine.output_bpm)
        self.assertEqual(engine.diagnostic_counts['tempo_hold_started:no_hand'],1)
        self.assertEqual(engine.diagnostic_counts['tempo_hold_ended:holding_deadline'],1)

    def test_missing_crossing_does_not_extend_old_evidence_expiry(self):
        engine=self.primed()
        count=engine.count
        engine.update(8,.5,1.01)
        self.assertIsNone(engine.update(8.05,.5,y_at(8.05)))
        self.assertEqual(engine.count,count)
        advance(engine,8.1,9.35)
        self.assertIsNone(engine.output_bpm)
        self.assertGreater(engine.diagnostic_counts['reset:tempo_expired'],0)

    def test_frame_gap_manual_reset_and_position_jump_never_hold(self):
        for action in ('frame_gap','reset','jump'):
            with self.subTest(action=action):
                engine=self.primed()
                if action=='frame_gap':
                    engine.update(8.3,None,None)
                elif action=='reset':
                    engine.update(8,None,None)
                    engine.reset()
                    self.assertEqual(engine.count,0)
                    self.assertEqual(engine.diagnostic_counts['reset:manual_reset'],1)
                else:
                    engine.update(8,.99,.99)
                self.assertIsNone(engine.output_bpm)
                self.assertEqual(len(engine.intervals),0)

    def test_duplicate_invalid_frame_cannot_start_hold(self):
        engine=self.primed()
        prior=engine.bpm
        engine.update(engine.last_input,.5,float('nan'))
        self.assertEqual(engine.bpm,prior)
        self.assertEqual(engine.tempo_state,'MEASURED')

    def test_held_oscillator_is_separate_from_detected_strokes(self):
        engine=self.primed()
        metro=TempoMetronome()
        metro.update(7.95,engine.output_bpm)
        count=engine.count
        engine.update(8,None,None)
        metro.update(8,engine.output_bpm)
        self.assertTrue(metro.running)
        self.assertEqual(engine.count,count)
        engine.expire(8.121)
        metro.update(8.121,engine.output_bpm)
        self.assertFalse(metro.running)

    def test_out_of_frame_coordinates_are_not_clamped_into_beats(self):
        engine=Conductor()
        for i in range(200):
            self.assertIsNone(engine.update(i/20,.5,1.01 if i%2 else -.01))
        self.assertEqual(engine.count,0)
        self.assertIsNone(engine.output_bpm)

    def test_quarter_note_contact_keeps_range_at_observed_slow_intervals(self):
        for bpm in (28,33,39):
            engine=Conductor()
            events=advance(engine,0,20,bpm=bpm)
            self.assertGreaterEqual(len(events),int(20*bpm/60)-1)
            self.assertIsNone(engine.output_bpm)
            self.assertGreater(engine.diagnostic_counts['interval_rejected:outside_bpm_range'],0)
            self.assertAlmostEqual(engine.config.arm_y,.56)
            self.assertEqual(engine.config.beat_y,.62)
        self.assertEqual((engine.config.min_bpm,engine.config.max_bpm),(40,180))

    def test_reference_60_to_90_with_brief_invalid_frames(self):
        for fps in (18,20,24):
            engine=Conductor()
            for i in range(24*fps):
                t=i/fps
                phase=t if t<10 else 10+1.5*(t-10)
                y=.51+.28*math.cos(2*math.pi*phase)
                engine.update(t,.5,1.01 if i in (7*fps,17*fps) else y)
                if i==9*fps:
                    self.assertAlmostEqual(engine.bpm,60,delta=.3)
            self.assertAlmostEqual(engine.bpm,90,delta=.3)

    def test_refractory_survives_discarding_the_interval_anchor(self):
        engine=Conductor(DetectorConfig(smoothing_tau_s=.005,max_position_jump=.6,
                                       reacquire_s=.02))
        advance(engine,0,8,bpm=120,fps=200)
        times=[engine.last_event.t]
        for i in range(1600,2000):
            t=i/200
            event=engine.update(t,.5,1.01 if i%31==0 else y_at(t,600))
            if event:
                times.append(event.t)
        self.assertGreater(len(times),3)
        self.assertTrue(all(b-a>=engine.config.refractory_s for a,b in zip(times,times[1:])))


class SelectionDiagnosticsTests(unittest.TestCase):
    def test_missing_metadata_is_not_dropped_by_zip_or_assigned_a_label(self):
        p=SimpleNamespace(x=.5,y=.5)
        for metadata in ([],[[]]):
            result=SimpleNamespace(hand_landmarks=[[p]],handedness=metadata)
            hands=hands_from_result(result)
            self.assertEqual(len(hands),1)
            self.assertIsNone(hands[0].label)
            selector=HandSelector()
            self.assertIs(selector.choose(hands,1),hands[0])
            self.assertIsNone(selector.previous.label)
            self.assertEqual(selector.last_details['unlabeled_hands'],1)

    def test_absent_conflicting_and_invalid_target_have_distinct_reasons(self):
        selector=HandSelector()
        left=Hand(.5,.5,'Left',.9)
        selector.choose([left],1)
        cases=[([], 'no_hands'),([Hand(.5,1.01,'Left',.9)],'landmark_out_of_frame'),
               ([Hand(.5,float('nan'),'Left',.9)],'landmark_nonfinite')]
        for i,(hands,reason) in enumerate(cases):
            self.assertIsNone(selector.choose(hands,1.02+i*.02))
            self.assertEqual(selector.last_reason,reason)
            self.assertEqual(selector.label,'Left')
            self.assertIs(selector.previous,left)
        self.assertIs(selector.choose([left],1.1),left)

    def test_no_switch_to_far_hand_during_target_dropout(self):
        selector=HandSelector()
        left=Hand(.1,.2,'Left',.9)
        selector.choose([left],1)
        for t in (1.05,1.1,1.2,2):
            self.assertIsNone(selector.choose([Hand(.9,.8,'Right',.99)],t))
            self.assertEqual(selector.label,'Left')
        for t in (2.05,2.1,2.15,2.2,2.25):
            self.assertIsNone(selector.choose([left],t))
        self.assertIs(selector.choose([left],2.31),left)

    def test_invalid_target_cannot_poison_motion_anchor(self):
        selector=HandSelector()
        original=Hand(.5,.5,'Left',.9)
        selector.choose([original],1)
        self.assertIsNone(selector.choose([Hand(4,8,'Left',.9)],1.05))
        self.assertIs(selector.previous,original)
        returned=Hand(.51,.52,'Left',.9)
        self.assertIs(selector.choose([returned],1.1),returned)


if __name__=='__main__':
    unittest.main()
