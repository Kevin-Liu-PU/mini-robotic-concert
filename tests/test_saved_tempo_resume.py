"""Camera-free manual-resume contracts; real clock and in-memory UI drawing."""
import unittest
from unittest.mock import patch
import numpy as np

from conductor.screen_clock import ScreenClock, ScreenClockConfig
from conductor.screen_display import render_clock
from tests.support.clocks import simulate, regular


def established(bpm=60, config=None):
    clock,_,_=simulate(regular(bpm,12),config=config)
    return clock


class SavedTempoResumeTests(unittest.TestCase):
    def test_60_and_120_resume_without_waiting_for_new_beats(self):
        for bpm in (60,120):
            with self.subTest(bpm=bpm):
                c=established(bpm);now=c.last_step;c.stop(now)
                c.step(now+.1,frame_time=now+.1,track_good=False)
                self.assertTrue(c.arm(now+.11));state=c.snapshot(now+.11)
                self.assertTrue(state['running']);self.assertAlmostEqual(state['bpm'],bpm)
                self.assertEqual(state['state'],'RESUMING_LAST_TEMPO')
                self.assertEqual(state['current_evidence_confidence'],0)
                self.assertEqual(state['phase_status'],'UNCONFIRMED')
                count=c.predicted_count
                c.step(now+.12,frame_time=now+.12,track_good=False)
                # A very recent old tick may impose the existing spacing limit.
                for i in range(1,101):c.step(now+.12+i*.01,frame_time=now+.12+i*.01,track_good=False)
                self.assertGreater(c.predicted_count,count)
                self.assertAlmostEqual(c.period,60/bpm)

    def test_space_stays_off_and_preserves_saved_bpm(self):
        c,trace,journal=simulate(regular(60,30),controls=((10.7,'stop'),))
        self.assertTrue(all(not r['running'] and r['bpm']==60 for r in trace if r['time_s']>=10.7))
        self.assertFalse(any(e['kind']=='PREDICTED_CLOCK_TICK' and e['time_s']>=10.7 for e in journal))

    def test_tempo_update_requires_three_fresh_intervals_after_resume(self):
        beats=regular(60,10)+regular(120,10,start=15.5)
        c,trace,journal=simulate(beats,controls=((10.7,'stop'),(15,'arm')))
        self.assertTrue(all(r['bpm']==60 and r['running'] for r in trace if 15<=r['time_s']<17.04))
        event=next(e for e in journal if e.get('reason')=='CONFIRMED_RESUMED_TEMPO')
        self.assertAlmostEqual(event['time_s'],17.04);self.assertAlmostEqual(event['bpm'],120)

    def test_irregular_return_keeps_saved_tempo_and_zero_current_support(self):
        beats=regular(60,10)+[15.5,15.85,17.2,17.6,18.9,19.3]
        c,trace,journal=simulate(beats,duration=21,controls=((10.7,'stop'),(15,'arm')))
        self.assertTrue(c.resume_pending);self.assertEqual(c.period,1)
        self.assertTrue(all(r['current_evidence_confidence']==0 for r in trace if r['time_s']>=15))
        self.assertFalse(any(e.get('reason')=='CONFIRMED_RESUMED_TEMPO' for e in journal))

    def test_visual_loss_resets_fresh_intervals_without_stopping_saved_cadence(self):
        c,trace,journal=simulate(regular(60,10)+regular(60,8,start=15.5),
            controls=((10.7,'stop'),(15,'arm')),lost=((17.1,17.2),))
        event=next(e for e in journal if e.get('reason')=='CONFIRMED_RESUMED_TEMPO')
        self.assertAlmostEqual(event['time_s'],20.54)
        self.assertTrue(all(r['running'] and r['bpm']==60 for r in trace if 15<=r['time_s']<20.54))

    def test_configured_eight_intervals_can_confirm_resume(self):
        cfg=ScreenClockConfig(initial_intervals=8)
        c,_,journal=simulate(regular(60,35),config=cfg,controls=((12,'stop'),(16,'arm')))
        event=next(e for e in journal if e.get('reason')=='CONFIRMED_RESUMED_TEMPO')
        self.assertAlmostEqual(event['time_s'],24.54)
        self.assertFalse(c.resume_pending)

    def test_no_saved_tempo_still_needs_initial_acquisition(self):
        c=ScreenClock();c.step(0,frame_time=0);c.stop(.01);self.assertTrue(c.arm(.02))
        self.assertFalse(c.running);self.assertIsNone(c.period)

    def test_reset_forgets_saved_tempo(self):
        c=established();now=c.last_step;c.reset(now)
        self.assertTrue(c.arm(now+.01));self.assertIsNone(c.period);self.assertFalse(c.running)

    def test_main_changes_do_not_resume_old_tempo(self):
        for reason in ('MAIN_SELECTION_CHANGED',):
            c=established();now=c.last_step;c.stop(now,reason)
            self.assertTrue(c.arm(now+.01));self.assertIsNone(c.period);self.assertFalse(c.running)

    def test_fault_recovery_never_resumes_automatically_or_reuses_old_tempo(self):
        c=established();now=c.last_step;c.fail(now,'CAMERA_ERROR')
        c.step(now+.1,frame_time=now+.1,track_good=False)
        self.assertFalse(c.running);self.assertEqual(c.fault_reason,'CAMERA_ERROR')
        c.stop(now+.11)  # Space cannot disguise the existing fault as a warm stop.
        self.assertTrue(c.arm(now+.12));self.assertIsNone(c.period);self.assertFalse(c.running)

    def test_unhealthy_enter_does_not_resume_saved_tempo(self):
        c=established();now=c.last_step;c.stop(now)
        c.step(now+.6)
        self.assertFalse(c.arm(now+.61));self.assertFalse(c.running)

    def test_fault_while_resuming_latches_off(self):
        c=established();now=c.last_step;c.stop(now);c.arm(now+.01)
        c.step(now+.02,frame_time=now+.02,fault='CAMERA_ERROR')
        count=c.predicted_count
        c.step(now+.03,frame_time=now+.03)
        self.assertEqual(c.predicted_count,count);self.assertFalse(c.running)

    def test_old_or_duplicate_events_cannot_supply_fresh_evidence(self):
        c=established();now=c.last_step;c.stop(now);c.arm(now+.01)
        c.step(now+.02,frame_time=now+.02,track_good=True,beat_time=now)
        self.assertIsNone(c.raw_anchor)
        c.step(now+.03,frame_time=now+.03,track_good=True,beat_time=now+.025)
        anchor=c.raw_anchor
        c.step(now+.04,frame_time=now+.03,track_good=True,beat_time=now+.035)
        self.assertEqual(c.raw_anchor,anchor);self.assertEqual(len(c.return_intervals),0)

    def test_quick_stop_resume_respects_spacing_and_does_not_catch_up(self):
        c=established(120);now=c.last_step
        c.step(now+.01,frame_time=now+.01)
        c.stop(now+.02);last_tick=c.last_tick;c.arm(now+.03)
        self.assertGreaterEqual(c.next_tick,last_tick+c.cfg.min_tick_spacing*c.period)
        c.drain_events()
        for i in range(4,105):c.step(now+i*.01,frame_time=now+i*.01)
        ticks=[e['time_s'] for e in c.drain_events() if e['kind']=='PREDICTED_CLOCK_TICK']
        self.assertTrue(all(b-a>=.3-1e-8 for a,b in zip([last_tick]+ticks,ticks)))

    def test_repeated_enter_does_not_clear_active_saved_resume(self):
        c=established();now=c.last_step;c.stop(now);c.arm(now+.01)
        self.assertFalse(c.arm(now+.02));self.assertTrue(c.running);self.assertEqual(c.period,1)

    def test_repeated_space_enter_can_resume_same_saved_tempo(self):
        c=established();now=c.last_step
        for i in range(3):
            t=now+.05*i;c.step(t+.01,frame_time=t+.01,track_good=False)
            c.stop(t+.02);self.assertTrue(c.arm(t+.03));self.assertEqual(c.period,1)

    def test_overlay_labels_saved_tempo_and_fresh_evidence(self):
        c=established();now=c.last_step;c.stop(now)
        for resume in (False,True):
            if resume:c.arm(now+.01)
            with patch('conductor.screen_display.text') as draw:
                render_clock(np.zeros((800,1360,3),dtype=np.uint8),c.snapshot(now+.02),now+.02)
            labels=[str(call.args[1]) for call in draw.call_args_list]
            self.assertIn('LAST STABLE TEMPO',labels)
            self.assertIn('60.0',labels)
            if resume:
                self.assertIn('RESUMING LAST TEMPO',labels)
                self.assertIn('New intervals: 0/3 | saved BPM',labels)
            else:self.assertIn('STOPPED - Enter resumes saved BPM; R clears',labels)


if __name__=='__main__':unittest.main()
