import math
import unittest
from conductor.screen_clock import ScreenClock, ScreenClockConfig
from conductor.app import parse_args
from tests.support.clocks import simulate, regular, case, metrics


class ScreenClockTests(unittest.TestCase):
    def test_three_consistent_intervals_required(self):
        c,trace,_=simulate(regular(60,6))
        self.assertTrue(all(not r['running'] for r in trace if r['time_s']<3.54))
        self.assertAlmostEqual(c.period,1)

    def test_irregular_bootstrap_cannot_start(self):
        c,_,_=simulate([.5,1,2.4,2.8,4.2,4.6,6,6.4])
        self.assertFalse(c.running)

    def test_initial_evidence_does_not_bridge_hand_loss(self):
        c,trace,_=simulate(regular(60,6),lost=((2.6,2.8),))
        self.assertFalse(c.running)

    def test_out_of_range_observations_never_change_tempo_beyond_bounds(self):
        for old,new in ((180,200),(40,36)):
            item=case('out_of_range',[60/old]*10+[60/new]*15)
            c,trace,_=simulate(item['beats'])
            self.assertTrue(all(40-1e-8<=r['bpm']<=180+1e-8 for r in trace if r['bpm']))

    def test_regular_tempos(self):
        for bpm in (40,60,120,180):
            with self.subTest(bpm=bpm):
                c,_,_=simulate(regular(bpm,30))
                self.assertAlmostEqual(60/c.period,bpm,places=5)

    def test_visible_pause_keeps_last_bpm_for_a_minute(self):
        c,trace,journal=simulate(regular(60,10),duration=75)
        self.assertTrue(c.running)
        self.assertEqual(trace[-1]['state'],'HOLDING_LAST_TEMPO')
        self.assertAlmostEqual(trace[-1]['bpm'],60)
        self.assertEqual(c.observed_count,10)
        self.assertGreater(c.predicted_count,60)
        self.assertTrue(all(r['bpm']==60 for r in trace if r['time_s']>10))

    def test_hand_exit_does_not_stop_healthy_clock(self):
        c,trace,_=simulate(regular(120,12),duration=70,lost=((6,71),))
        self.assertTrue(c.running)
        self.assertEqual(trace[-1]['state'],'HOLDING_LAST_TEMPO')
        self.assertEqual(c.observed_count,12)

    def test_isolated_two_period_interval_does_not_halve(self):
        for bpm in (40,60,120):
            beats=regular(bpm,30);beats.pop(12)
            c,trace,_=simulate(beats)
            self.assertAlmostEqual(60/c.period,bpm)
            self.assertTrue(all(abs(r['bpm']-bpm)<1e-6 for r in trace if r['bpm']))

    def test_one_midpoint_extra_does_not_double(self):
        c,trace,_=simulate(sorted(regular(60,25)+[11.]))
        self.assertTrue(all(abs(r['bpm']-60)<1e-6 for r in trace if r['bpm']))
        self.assertEqual(c.observed_count,26)

    def test_confirmed_half_and_double_tempo(self):
        for old,new in ((60,120),(120,60),(120,40)):
            item=case('step', [60/old]*12+[60/new]*20)
            c,trace,_=simulate(item['beats'])
            self.assertAlmostEqual(60/c.period,new,places=2)
            self.assertTrue(all(r['running'] for r in trace if r['time_s']>10))

    def test_long_pause_reentry_does_not_use_pause_interval(self):
        beats=regular(60,10)+regular(60,10,start=30.85)
        c,trace,journal=simulate(beats)
        self.assertTrue(all(abs(r['bpm']-60)<1e-6 for r in trace if r['bpm']))
        self.assertEqual(c.observed_count,20)
        self.assertTrue(any(r.get('reason')=='CONFIRMED_RETURN' for r in journal))

    def test_phase_shift_recovery_within_four_seconds(self):
        for shift in (.35,-.35):
            item=case('phase_case',[1]*30,phase_shift=(12,shift))
            result,_,_=metrics(item)
            self.assertIsNotNone(result['phase_recovery_50ms_3beats_s'])
            self.assertLessEqual(result['phase_recovery_50ms_3beats_s'],4.05)

    def test_stop_latches_despite_new_beats(self):
        c,trace,journal=simulate(regular(60,30),controls=((10.7,'stop'),))
        self.assertFalse(c.running)
        self.assertEqual(c.observed_count,30)
        self.assertFalse(any(r['kind']=='PREDICTED_CLOCK_TICK' and r['time_s']>=10.7 for r in journal))

    def test_manual_arm_resumes_saved_tempo_then_confirms_new_four_beats(self):
        c,trace,journal=simulate(regular(60,30),controls=((10.7,'stop'),(15.7,'arm')))
        self.assertFalse(any(r['running'] for r in trace if 10.7 <= r['time_s'] < 15.7))
        self.assertTrue(all(r['running'] and r['bpm']==60 for r in trace if 15.7 <= r['time_s'] < 19.54))
        resumed=next(e for e in journal if e.get('reason')=='CONFIRMED_RESUMED_TEMPO')
        self.assertAlmostEqual(resumed['time_s'],19.54)
        self.assertTrue(c.running)

    def test_residual_old_event_after_arm_is_only_logged(self):
        c=ScreenClock();c.step(10,frame_time=10,track_good=True)
        c.stop(10.01);self.assertTrue(c.arm(10.02))
        c.step(10.05,frame_time=10.04,track_good=True,beat_time=10)
        self.assertEqual(c.observed_count,1)
        self.assertIsNone(c.raw_anchor)
        self.assertFalse(c.running)

    def test_fault_stays_stopped_after_automatic_recovery(self):
        c,trace,journal=simulate(regular(60,25),faults=((10.7,10.8,'CAMERA_ERROR'),))
        self.assertFalse(c.running)
        self.assertTrue(c.healthy)
        self.assertEqual(c.fault_reason,'CAMERA_ERROR')
        self.assertFalse(any(r['kind']=='PREDICTED_CLOCK_TICK' and r['time_s']>=10.7 for r in journal))

    def test_processing_stall_cannot_emit_catchup(self):
        c,_,journal=simulate(regular(120,50),stalls=((10.7,12.5),))
        self.assertEqual(c.fault_reason,'PROCESSING_HEARTBEAT_GAP')
        self.assertFalse(any(r['kind']=='PREDICTED_CLOCK_TICK' and r['time_s']>=10.7 for r in journal))

    def test_fresh_camera_does_not_mask_processing_timeout(self):
        c=ScreenClock();c.step(0,frame_time=0,track_good=True)
        for i in range(1,70):
            c.step(i/100,camera_time=i/100)
        self.assertEqual(c.fault_reason,'PROCESSING_HEARTBEAT_TIMEOUT')

    def test_reset_clears_tempo_but_keeps_stopped(self):
        c,_,_=simulate(regular(60,8))
        now=c.last_step
        c.reset(now)
        self.assertFalse(c.armed)
        self.assertIsNone(c.period)
        self.assertIsNone(c.last_accepted)
        self.assertEqual(c.observed_count,8)

    def test_long_shifted_return_realigns_with_new_interval_evidence(self):
        item=case('SYNTHETIC_long_pause_shifted_return',[1]*10+[15.35]+[1]*12)
        result,_,_=metrics(item)
        self.assertLessEqual(result['shifted_return_phase_recovery_50ms_3beats_s'],4.05)

    def test_duplicate_frame_cannot_refresh_health_or_add_beat(self):
        c=ScreenClock();c.step(0,frame_time=0,track_good=True,beat_time=0)
        for i in range(1,70):
            c.step(i/100,frame_time=0,track_good=True,beat_time=i/100)
        self.assertEqual(c.observed_count,1)
        self.assertEqual(c.fault_reason,'CAMERA_HEARTBEAT_TIMEOUT')

    def test_stale_processing_result_faults(self):
        c=ScreenClock();c.step(.6,frame_time=.6,track_good=True)
        c.step(1,frame_time=.7,track_good=True)
        self.assertEqual(c.fault_reason,'STALE_PROCESSING_RESULT')

    def test_system_fault_requires_healthy_before_arm(self):
        c=ScreenClock();c.step(1,fault='CAMERA_ERROR')
        self.assertFalse(c.arm(1.01))
        c.step(1.02,frame_time=1.01,track_good=False)
        self.assertTrue(c.arm(1.03))
        self.assertFalse(c.running)

    def test_duplicate_reversed_nan_times_do_not_mutate(self):
        c=ScreenClock();c.step(1,frame_time=1,track_good=True,beat_time=1)
        before=(c.observed_count,c.predicted_count,c.last_frame,c.last_step)
        for now in (1,.9,math.nan,math.inf):
            c.step(now,frame_time=now,track_good=True,beat_time=now)
        self.assertEqual(before,(c.observed_count,c.predicted_count,c.last_frame,c.last_step))

    def test_late_ui_tick_never_bursts(self):
        c,_,_=simulate(regular(180,30),duration=10.3)
        c.drain_events();base=c.last_step
        for i in range(1,30):
            now=base+i*.24
            c.step(now,frame_time=now,track_good=False)
        ticks=[r['time_s'] for r in c.drain_events() if r['kind']=='PREDICTED_CLOCK_TICK']
        self.assertTrue(all(b-a>=.20-1e-9 for a,b in zip(ticks,ticks[1:])))

    def test_config_rejects_unsafe_or_nonfinite_values(self):
        for options in ({'phase_gain':math.nan},{'phase_step_limit':1},{'heartbeat_timeout_s':0},
                        {'initial_intervals':2},{'period_alpha':2},{'reentry_periods':2}):
            with self.subTest(options=options),self.assertRaises(ValueError):ScreenClockConfig(**options)

    def test_hold_recording_is_explicit_and_still_requires_preview(self):
        args=parse_args(['--hold-clock','--record','manual-test'])
        self.assertTrue(args.hold_clock)
        self.assertEqual(args.record,'manual-test')
        with self.assertRaises(SystemExit):
            parse_args(['--hold-clock','--record','manual-test','--headless','--seconds','5'])


if __name__=='__main__':unittest.main()
