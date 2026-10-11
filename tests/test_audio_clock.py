"""Deterministic numeric buffers and fake output drivers only; never hardware."""
from dataclasses import replace
from threading import Event
from types import SimpleNamespace
import math
import sys
import time
import unittest
from unittest.mock import patch
import numpy as np

from conductor.audio_clock import AudioConfig, AudioPlan, AudioPlanner, ClickRenderer
from conductor.audio_output import AudioOutput, open_output_stream, sample_clock_bridge
from conductor.app import parse_args
from conductor.screen_clock import ScreenClock
from tests.support.clocks import simulate, regular


def established(bpm=60):return simulate(regular(bpm,12))[0]

def draw(renderer, start, frames=480, lead=.03, status=False):
    out=np.empty((frames,renderer.cfg.channels),dtype=np.float32)
    renderer.render(out,frames,now=start-lead,pa_current=100+start-lead,
                    pa_dac=100+start,status=status)
    return out


class RendererTests(unittest.TestCase):
    def renderer(self, ticks=((1,1.),), **kw):
        r=ClickRenderer(AudioConfig(**kw))
        r.plan=AudioPlan(1,0,100,True,1,.6,ticks)
        return r

    def test_sample_placement_and_peak_in_mono_and_stereo(self):
        for channels in (1,2):
            r=self.renderer(((1,1.004),),channels=channels)
            out=draw(r,1)
            self.assertTrue(np.all(out[:192]==0))
            self.assertGreater(np.max(np.abs(out[192:])),0)
            self.assertLessEqual(np.max(np.abs(out)),.08)
            event=r.drain_events()[0]
            self.assertAlmostEqual(event['host_estimated_output_time_s'],1.004)
            self.assertIsNone(event['acoustic_output_time_s'])
            self.assertLess(event['playback_request_time_s'],event['predicted_time_s'])
            if channels==2:np.testing.assert_array_equal(out[:,0],out[:,1])

    def test_buffer_boundary_does_not_consume_next_block_tick(self):
        r=self.renderer()
        draw(r,.99)
        draw(r,1.)
        self.assertEqual(r.requested_count,1)

    def test_split_click_matches_precomputed_waveform(self):
        r=self.renderer(((1,1.009),))
        out=np.concatenate([draw(r,1+i*.01)[:,0] for i in range(4)])
        np.testing.assert_allclose(out[432:432+len(r.wave)],r.wave,atol=1e-6)
        self.assertEqual(r.requested_count,1)

    def test_same_prediction_revision_cannot_replay_committed_click(self):
        r=self.renderer();draw(r,1)
        r.plan=replace(r.plan,ticks=((1,1.02),))
        draw(r,1.01);draw(r,1.02)
        self.assertEqual(r.requested_count,1)

    def test_uncommitted_phase_update_moves_request(self):
        r=self.renderer(((1,1.10),));draw(r,1)
        r.plan=replace(r.plan,ticks=((1,1.025),))
        draw(r,1.01);draw(r,1.02)
        self.assertAlmostEqual(r.drain_events()[0]['host_estimated_output_time_s'],1.025)

    def test_late_tick_skipped_without_catchup(self):
        r=self.renderer(((1,.9),(2,1.2)))
        self.assertFalse(draw(r,1).any())
        self.assertEqual(r.skipped_count,1)
        self.assertEqual(r.drain_events()[0]['kind'],'AUDIO_SKIPPED_LATE')

    def test_warm_resume_requests_earliest_buffer_without_claiming_phase(self):
        r=self.renderer(((1,1.),))
        r.plan=replace(r.plan,immediate_sequence=1)
        draw(r,1.06,lead=.05)
        e=r.drain_events()[0]
        self.assertEqual(e['kind'],'AUDIO_PLAYBACK_REQUEST')
        self.assertTrue(e['immediate_resume'])
        self.assertAlmostEqual(e['requested_alignment_error_s'],.06)

    def test_fresh_phase_advance_inside_driver_lead_is_requested_not_dropped(self):
        r=self.renderer(((1,1.),))
        draw(r,1.04,lead=.03)
        e=r.drain_events()[0]
        self.assertEqual(e['kind'],'AUDIO_PLAYBACK_REQUEST')
        self.assertFalse(e['immediate_resume'])
        self.assertAlmostEqual(e['requested_alignment_error_s'],.04)

    def test_old_warm_resume_is_not_replayed_after_stall(self):
        r=self.renderer(((1,1.),));r.plan=replace(r.plan,immediate_sequence=1)
        draw(r,1.1,lead=.03)
        self.assertEqual(r.requested_count,0)

    def test_stop_cancels_click_tail_and_future_requests(self):
        r=self.renderer();draw(r,1)
        r.plan=replace(r.plan,generation=2,active=False)
        self.assertFalse(draw(r,1.01).any())
        self.assertFalse(draw(r,1.02).any())
        self.assertEqual(r.requested_count,1)

    def test_close_spacing_cannot_create_double_click(self):
        r=self.renderer(((1,1.),(2,1.01)))
        draw(r,1);draw(r,1.01)
        self.assertEqual(r.requested_count,1)
        self.assertEqual(r.drain_events()[-1]['kind'],'AUDIO_SKIPPED_SPACING')

    def test_source_watchdog_latches_even_when_plan_recovers(self):
        r=self.renderer();r.plan=replace(r.plan,valid_until=.9)
        self.assertFalse(draw(r,1).any())
        self.assertEqual(r.fault,'AUDIO_SOURCE_HEARTBEAT_EXPIRED')
        r.plan=replace(r.plan,valid_until=100)
        self.assertFalse(draw(r,1.01).any())

    def test_no_samples_scheduled_past_source_deadline(self):
        r=self.renderer(((1,1.004),));r.plan=replace(r.plan,valid_until=1.006)
        out=draw(r,1)
        self.assertTrue(out[:288].any())
        self.assertFalse(out[288:].any())

    def test_bad_driver_status_and_host_time_fail_silent(self):
        cases=[dict(now=1,pa_current=100,pa_dac=100,status=True),
               dict(now=1,pa_current=0,pa_dac=0),
               dict(now=1,pa_current=100,pa_dac=101),
               dict(now=1,pa_current=100,pa_dac=math.nan)]
        for args in cases:
            r=self.renderer();out=np.ones((480,1),dtype=np.float32)
            r.render(out,480,**args)
            self.assertIsNotNone(r.fault);self.assertFalse(out.any())

    def test_driver_time_rewind_latches_fault(self):
        r=self.renderer();draw(r,1);draw(r,.99)
        self.assertEqual(r.fault,'AUDIO_HOST_TIME_DISCONTINUITY')

    def test_callback_arrival_jitter_does_not_rewind_continuous_host_clock(self):
        r=self.renderer(((1,1.025),))
        out=np.empty((480,1),dtype=np.float32)
        for i,jitter in enumerate((0,.012,0,0,.008,0)):
            now=.97+i*.01+jitter
            r.render(out,480,now=now,pa_current=100.97+i*.01,pa_dac=101+i*.01)
        self.assertIsNone(r.fault)
        self.assertEqual(r.callback_count,6)
        self.assertAlmostEqual(r.drain_events()[0]['host_estimated_output_time_s'],1.025)

    def test_precalibrated_bridge_does_not_include_first_callback_delay(self):
        r=self.renderer(((1,1.004),));r.clock_offset_s=-100.
        out=np.empty((480,1),dtype=np.float32)
        r.render(out,480,now=.982,pa_current=100.97,pa_dac=101.)
        self.assertIsNone(r.fault)
        self.assertAlmostEqual(r.drain_events()[0]['host_estimated_output_time_s'],1.004)

    def test_missed_output_deadline_has_distinct_fault_and_raw_evidence(self):
        r=self.renderer();r.clock_offset_s=-100.
        out=np.empty((480,1),dtype=np.float32)
        r.render(out,480,now=1.02,pa_current=100.97,pa_dac=101.)
        self.assertEqual(r.fault,'AUDIO_CALLBACK_DEADLINE_OR_BRIDGE_ERROR')
        self.assertEqual(r.fault_timing[:3],(1.02,100.97,101.))
        self.assertFalse(out.any())

    def test_evidence_overflow_is_explicit_and_silent(self):
        r=ClickRenderer(AudioConfig(),ring_capacity=1)
        r.plan=AudioPlan(1,0,100,True,1,.6,((1,0.),))
        draw(r,0)
        for i in range(1,101):
            r.plan=replace(r.plan,ticks=((2,1.),));out=draw(r,i*.01)
        self.assertEqual(r.fault,'AUDIO_EVIDENCE_OVERFLOW');self.assertFalse(out.any())

    def test_regular_60_120_and_tempo_change_buffers(self):
        for targets in ([1+i for i in range(10)],[1+i*.5 for i in range(16)],
                        [1+i for i in range(5)]+[6+i*.5 for i in range(8)]):
            r=self.renderer(())
            for i in range(1100):
                t=i*.01
                upcoming=[(n+1,v) for n,v in enumerate(targets) if t-.03<=v<t+.3]
                period=.5 if len(targets)>10 and t>=6 else min(np.diff(targets))
                r.plan=AudioPlan(1,t,t+.5,True,float(period),.6*period,tuple(upcoming))
                draw(r,t)
            events=r.drain_events()
            self.assertIsNone(r.fault)
            self.assertEqual(len(events),len(targets))
            self.assertTrue(all(e['kind']=='AUDIO_PLAYBACK_REQUEST' for e in events))
            self.assertLess(max(abs(e['requested_alignment_error_s']) for e in events),1/48000)


class PlannerTests(unittest.TestCase):
    def test_initially_muted(self):
        c=established();p=AudioPlanner()
        self.assertFalse(p.publish(c,c.last_step,c.drain_events()).active)

    def test_tracking_loss_keeps_plan_active_and_period(self):
        c=established();p=AudioPlanner();c.drain_events();p.toggle_mute(c.last_step)
        c.step(c.last_step+.1,frame_time=c.last_step+.1,track_good=False)
        plan=p.publish(c,c.last_step,c.drain_events())
        self.assertTrue(plan.active);self.assertEqual(plan.period,1)

    def test_space_enter_preserves_tempo_and_reset_clears(self):
        c=established();p=AudioPlanner();c.drain_events();p.toggle_mute(c.last_step)
        c.stop(c.last_step+.01)
        stopped=p.publish(c,c.last_step+.01,c.drain_events())
        self.assertFalse(stopped.active)
        c.arm(c.last_step+.02)
        resumed=p.publish(c,c.last_step+.02,c.drain_events())
        self.assertTrue(resumed.active);self.assertEqual(resumed.period,1)
        self.assertGreater(resumed.generation,stopped.generation)
        self.assertEqual(resumed.immediate_sequence,c.predicted_count+1)
        c.reset(c.last_step+.03)
        self.assertFalse(p.publish(c,c.last_step+.03,c.drain_events()).active)
        self.assertIsNone(c.period)

    def test_fault_recovery_does_not_auto_enable_audio(self):
        c=established();p=AudioPlanner();c.drain_events();p.toggle_mute(c.last_step)
        c.fail(c.last_step,'CAMERA_ERROR')
        self.assertFalse(p.publish(c,c.last_step,c.drain_events()).active)
        c.step(c.last_step+.1,frame_time=c.last_step+.1)
        self.assertFalse(p.publish(c,c.last_step,c.drain_events()).active)

    def test_batched_arm_and_tick_keep_the_same_immediate_prediction_id(self):
        c=established();p=AudioPlanner();c.drain_events();p.toggle_mute(c.last_step)
        now=c.last_step;c.stop(now)
        for delta in (.2,.4,.6):c.step(now+delta,frame_time=now+delta,track_good=False)
        c.arm(now+.61)
        c.step(now+.62,frame_time=now+.62,track_good=False)
        plan=p.publish(c,now+.62,c.drain_events())
        self.assertEqual(plan.immediate_sequence,c.predicted_count)
        self.assertEqual(plan.ticks[0][0],plan.immediate_sequence)

    def test_mute_unmute_does_not_replay_past_tick(self):
        c=established();p=AudioPlanner();events=c.drain_events();now=c.last_step
        p.publish(c,now,events);p.toggle_mute(now)
        self.assertTrue(all(t>=now for _,t in p.publish(c,now).ticks))

    def test_duplicate_frames_do_not_renew_audio_source_deadline(self):
        c=established();p=AudioPlanner();c.drain_events();p.toggle_mute(c.last_step)
        original=p.publish(c,c.last_step).valid_until
        old=c.last_frame;c.step(c.last_step+.2,frame_time=old,track_good=True)
        self.assertEqual(p.publish(c,c.last_step).valid_until,original)

    def test_isolated_missed_beat_does_not_halve_audio_tempo(self):
        c,trace,events=simulate([t for t in regular(60,18) if t!=10.5])
        self.assertTrue(all(r['bpm']==60 for r in trace if r['time_s']>5))
        self.assertTrue(any(e.get('reason')=='LONG_OR_OUT_OF_RANGE_KEEP_TEMPO' for e in events))
        p=AudioPlanner();p.toggle_mute(c.last_step)
        self.assertEqual(p.publish(c,c.last_step).period,1)


class FakeStream:
    def __init__(self):
        self.device=7;self.samplerate=48000;self.latency=.03
        self.active=False;self.started=Event();self.closed=False;self.aborted=False
    def start(self):self.active=True;self.started.set()
    def abort(self):self.aborted=True;self.active=False
    def close(self):self.closed=True
    @property
    def time(self):return time.perf_counter()+100


class LifecycleTests(unittest.TestCase):
    def test_selected_output_name_guard_refuses_wrong_device_before_open(self):
        calls=[]
        fake=SimpleNamespace(query_devices=lambda *a:{'name':'Unapproved USB speaker','hostapi':0},
                             OutputStream=lambda **kw:calls.append(kw))
        with patch.dict(sys.modules,{'sounddevice':fake}):
            with self.assertRaises(RuntimeError):
                open_output_stream(AudioConfig(device=4,device_name_contains='Example Output'),lambda *a:None)
        self.assertFalse(calls)

    def test_selected_output_name_and_api_are_reported(self):
        fake=SimpleNamespace(query_devices=lambda *a:{'name':'Example Output','hostapi':0},
            query_hostapis=lambda *a:{'name':'MME'},OutputStream=lambda **kw:FakeStream())
        with patch.dict(sys.modules,{'sounddevice':fake}):
            stream=open_output_stream(AudioConfig(device=4,device_name_contains='Example Output'),lambda *a:None)
        self.assertEqual(stream.conductor_verified_output,{'name':'Example Output','host_api':'MME'})

    def test_manual_check_plan_requests_exactly_one_click(self):
        from tests.support.replay import single_click_plan
        r=ClickRenderer(AudioConfig());r.plan=single_click_plan(1.)
        for i in range(30):draw(r,1.+i*.01)
        requests=[e for e in r.drain_events() if e['kind']=='AUDIO_PLAYBACK_REQUEST']
        self.assertEqual(len(requests),1)
        self.assertAlmostEqual(requests[0]['predicted_time_s'],1.1)

    def test_bridge_uses_shortest_bracket_and_reports_uncertainty(self):
        samples=iter([10,10.002,11,11.0002,12,12.003,13,13.002,14,14.001])
        class Stream:
            times=iter([110.001,111.0001,112.0015,113.001,114.0005])
            @property
            def time(self):return next(self.times)
        bridge=sample_clock_bridge(Stream(),lambda:next(samples))
        self.assertAlmostEqual(bridge['offset_s'],-100)
        self.assertAlmostEqual(bridge['sampling_uncertainty_s'],.0001)
        self.assertFalse(bridge['acoustic_calibration'])

    def test_bridge_rejects_unavailable_clock(self):
        with self.assertRaises(RuntimeError):sample_clock_bridge(SimpleNamespace(time=0),time.perf_counter)
    def test_real_factory_requests_output_only_callback_stream(self):
        calls=[]
        fake=SimpleNamespace(OutputStream=lambda **kw:calls.append(kw) or FakeStream())
        with patch.dict(sys.modules,{'sounddevice':fake}):
            open_output_stream(AudioConfig(),lambda *a:None)
        self.assertEqual(calls[0]['channels'],1)
        self.assertEqual(calls[0]['blocksize'],0)
        self.assertEqual(calls[0]['dtype'],'float32')

    def test_nonblocking_open_and_close_after_late_completion(self):
        entered=Event();release=Event();stream=FakeStream()
        def factory(*args):entered.set();release.wait(1);return stream
        a=AudioOutput(AudioConfig(),stream_factory=factory)
        self.assertTrue(entered.wait(.5));self.assertFalse(a.ready)
        a.done.set();release.set();self.assertTrue(a.close())
        self.assertTrue(stream.closed);self.assertFalse(stream.started.is_set())

    def test_ready_mute_callback_and_clean_close(self):
        stream=FakeStream();a=AudioOutput(AudioConfig(),stream_factory=lambda *args:stream)
        self.assertTrue(stream.started.wait(.5))
        a.thread.join(.03)
        self.assertEqual(a.snapshot()['state'],'MUTED')
        self.assertTrue(a.toggle_mute(time.perf_counter()))
        self.assertTrue(a.close());self.assertTrue(stream.aborted);self.assertTrue(stream.closed)

    def test_open_failure_never_falls_back_to_another_device(self):
        calls=[]
        def fail(*args):calls.append(1);raise RuntimeError('fake unavailable output')
        a=AudioOutput(AudioConfig(),stream_factory=fail);a.thread.join(.5)
        self.assertEqual(calls,[1]);self.assertIsNotNone(a.fault)
        self.assertFalse(a.toggle_mute(time.perf_counter()));self.assertTrue(a.close())

    def test_abort_failure_still_attempts_close_and_reports_failure(self):
        stream=FakeStream()
        def fail_abort():raise RuntimeError('fake abort failure')
        stream.abort=fail_abort
        a=AudioOutput(AudioConfig(),stream_factory=lambda *args:stream)
        self.assertTrue(stream.started.wait(.5))
        self.assertFalse(a.close());self.assertTrue(stream.closed)
        self.assertEqual(a.fault,'AUDIO_CLOSE_FAILED')

    def test_callback_exception_becomes_visible_silent_fault(self):
        stream=FakeStream();a=AudioOutput(AudioConfig(),stream_factory=lambda *args:stream)
        stream.started.wait(.5);out=np.ones((480,1),dtype=np.float32)
        a._callback(out,480,SimpleNamespace(),False)
        self.assertFalse(out.any());self.assertEqual(a.fault,'AUDIO_CALLBACK_ERROR');a.close()

    def test_cli_requires_explicit_opt_in_and_preview(self):
        self.assertFalse(parse_args([]).audio_clock)
        with patch('sys.stderr'):
            for args in (['--audio-clock'],['--audio-config','x'],['--audio-clock','--hold-clock','--headless','--seconds','1']):
                with self.assertRaises(SystemExit):parse_args(args)
        self.assertTrue(parse_args(['--hold-clock','--audio-clock']).audio_clock)

    def test_config_rejects_nonfinite_and_unsafe_ranges(self):
        for args in (dict(peak_amplitude=.9),dict(peak_amplitude=math.nan),dict(channels=True),
                     dict(device=-1),dict(sample_rate=12),dict(max_output_lead_s=1)):
            with self.assertRaises(ValueError):AudioConfig(**args)


if __name__=='__main__':unittest.main()
