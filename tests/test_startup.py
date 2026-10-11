"""Offline startup regressions. No camera/model construction or real key input."""
from pathlib import Path
from threading import Event
import unittest
from unittest.mock import patch
import numpy as np

from conductor.screen_clock import ScreenClock, ScreenClockConfig
from conductor.startup import ResourceStartup, camera_fault, recording_gate
from conductor.screen_display import render_clock
from conductor.display import render
from conductor.settings import load_config
from conductor.core import DetectorConfig, TempoMetronome
from conductor.multitrack import ConductorSession


class StartupTests(unittest.TestCase):
    def test_actual_7_second_first_frame_timeline_no_false_fault(self):
        c=ScreenClock();c.begin_startup(0)
        for i in range(707):
            state=c.step(i/100)
            self.assertEqual(state['state'],'STARTING')
            self.assertFalse(state['healthy'])
            self.assertFalse(state['running'])
            self.assertEqual(c.predicted_count,0)
        state=c.step(7.0768984,frame_time=7.0260646,track_good=False)
        self.assertEqual(state['state'],'ACQUIRING')
        self.assertTrue(state['healthy']);self.assertTrue(state['armed'])
        self.assertIsNone(c.fault_reason)
        self.assertEqual(c.first_valid_frame_at,7.0768984)

    def test_first_frame_after_model_wait_does_not_count_as_runtime_gap(self):
        c=ScreenClock();c.begin_startup(0);c.step(.01)
        state=c.step(12,frame_time=11.99,track_good=False)
        self.assertEqual(state['state'],'ACQUIRING')
        self.assertIsNone(c.fault_reason)

    def test_never_arriving_first_frame_times_out_at_startup_deadline(self):
        c=ScreenClock();c.begin_startup(10)
        self.assertEqual(c.step(24.99)['state'],'STARTING')
        state=c.step(25)
        self.assertEqual(state['fault'],'STARTUP_TIMEOUT')
        self.assertFalse(state['healthy']);self.assertFalse(state['armed'])

    def test_late_first_frame_does_not_erase_timeout(self):
        c=ScreenClock();c.begin_startup(0)
        state=c.step(16,frame_time=15.99,track_good=False)
        self.assertEqual(state['fault'],'STARTUP_TIMEOUT')
        state=c.step(16.01,frame_time=16,track_good=False)
        self.assertTrue(state['healthy']);self.assertFalse(state['armed'])
        self.assertTrue(c.arm(16.02));self.assertFalse(c.running)

    def test_fresh_camera_without_processed_model_result_is_not_ready(self):
        c=ScreenClock();c.begin_startup(0)
        for i in range(1601):c.step(i/100,camera_time=i/100)
        self.assertEqual(c.fault_reason,'STARTUP_TIMEOUT')
        self.assertFalse(c.healthy);self.assertIsNone(c.first_valid_frame_at)

    def test_initial_stale_result_waits_for_valid_frame(self):
        c=ScreenClock();c.begin_startup(0)
        self.assertEqual(c.step(1,frame_time=.7)['state'],'STARTING')
        self.assertFalse(c.healthy)
        self.assertIsNone(c.fault_reason)
        self.assertEqual(c.step(1.01,frame_time=1)['state'],'ACQUIRING')

    def test_runtime_stale_results_still_fault_immediately(self):
        c=ScreenClock();c.step(7,frame_time=6.99)
        c.step(7.4,frame_time=7.1)
        self.assertEqual(c.fault_reason,'STALE_PROCESSING_RESULT')

    def test_runtime_camera_timeout_not_increased(self):
        c=ScreenClock();c.begin_startup(0);c.step(7.08,frame_time=7.026)
        for i in range(709,759):c.step(i/100)
        self.assertEqual(c.fault_reason,'CAMERA_HEARTBEAT_TIMEOUT')
        self.assertEqual(c.cfg.heartbeat_timeout_s,.5)

    def test_runtime_processing_block_still_faults_with_fresh_return(self):
        c=ScreenClock();c.step(7.08,frame_time=7.026)
        c.step(7.7,frame_time=7.69)
        self.assertEqual(c.fault_reason,'PROCESSING_HEARTBEAT_GAP')

    def test_initial_failure_is_immediate_and_latched(self):
        c=ScreenClock();c.begin_startup(0);c.step(.1,fault='MODEL_INITIALIZATION_FAILED')
        self.assertEqual(c.fault_reason,'MODEL_INITIALIZATION_FAILED')
        state=c.step(.2,frame_time=.19)
        self.assertTrue(state['healthy']);self.assertFalse(state['armed'])
        self.assertTrue(c.arm(.21));self.assertFalse(c.running)

    def test_space_during_startup_survives_first_frame(self):
        c=ScreenClock();c.begin_startup(0);c.step(.1);c.stop(.2)
        c.step(7.08,frame_time=7.026,track_good=True,beat_time=7.026)
        self.assertFalse(c.armed);self.assertIsNone(c.period)
        self.assertEqual(c.snapshot(7.08)['state'],'STOPPED')
        self.assertTrue(c.arm(7.09));self.assertIsNone(c.raw_anchor)

    def test_enter_during_startup_cannot_fake_readiness(self):
        c=ScreenClock();c.begin_startup(0);c.step(.1)
        self.assertFalse(c.arm(.2));self.assertFalse(c.healthy)
        self.assertEqual(c.snapshot(.2)['state'],'STARTING')

    def test_invalid_first_timestamp_never_initializes(self):
        for stamp in (float('nan'),float('inf'),2):
            c=ScreenClock();c.begin_startup(0)
            self.assertEqual(c.step(1,frame_time=stamp)['state'],'STARTING')
            self.assertFalse(c.healthy)

    def test_startup_budget_cannot_be_reset(self):
        c=ScreenClock();c.begin_startup(0);c.begin_startup(14)
        self.assertEqual(c.step(15)['fault'],'STARTUP_TIMEOUT')

    def test_config_validates_separate_budget(self):
        for value in (0,61,float('inf'),True):
            with self.assertRaises(ValueError):ScreenClockConfig(startup_timeout_s=value)

    def test_capture_failure_vs_startup_fallback(self):
        self.assertEqual(camera_fault({'worker_alive':False,'status':'OPENING CAMERA'},False),'CAMERA_WORKER_UNAVAILABLE')
        self.assertEqual(camera_fault({'worker_alive':True,'status':'CAMERA ERROR'},False),'CAMERA_WORKER_UNAVAILABLE')
        self.assertIsNone(camera_fault({'worker_alive':True,'status':'REOPENING CAMERA'},False))
        self.assertEqual(camera_fault({'worker_alive':True,'status':'REOPENING CAMERA'},True),'CAMERA_WORKER_UNAVAILABLE')

    def test_recording_ready_requires_first_valid_frame_and_armed_clock(self):
        c=ScreenClock();c.step(0)
        self.assertEqual(recording_gate(c.snapshot(0),False),(False,'Starting camera/model - please wait'))
        c.step(.1,frame_time=.09)
        self.assertEqual(recording_gate(c.snapshot(.1),True),(True,None))
        c.stop(.11)
        self.assertEqual(recording_gate(c.snapshot(.11),True),(False,'Clock stopped - Enter before REC'))
        c.fail(.12,'TEST_FAILURE')
        self.assertEqual(recording_gate(c.snapshot(.12),True),(False,'Clock fault - Enter after recovery'))

    def test_starting_ui_does_not_claim_recording_ready(self):
        c=ScreenClock();state=c.step(1)
        cfg=load_config('config-screen-hold.json');session=ConductorSession(DetectorConfig(**cfg['detector']))
        info=dict(demo=False,source_starting=True,source_fault=None,capture_fps=0,processing_fps=0,
                  inference_ms=0,hand_label='Waiting',primary=None,primary_status='Waiting',
                  camera_mode='pending',camera_name='Synthetic test',camera_status='Starting camera/model',
                  recording={'state':'READY','dropped_frames':0,'diagnostic_drops':0},recording_source_ready=False,
                  recording_wait_message='Starting camera/model - please wait')
        with patch('conductor.display.text') as draw:
            canvas=render(None,session.engine,TempoMetronome(),1,session.trajectories,[],cfg,info)
            labels=[str(call.args[1]) for call in draw.call_args_list]
        self.assertIn('STARTING CAMERA / MODEL',labels)
        self.assertIn('Starting camera/model - please wait',labels)
        self.assertNotIn('Recording READY - press C',labels)
        with patch('conductor.screen_display.text') as draw:
            render_clock(canvas,state,1,recording_enabled=True)
            self.assertTrue(any('Starting camera/model' in str(call.args[1]) for call in draw.call_args_list))


class ResourceStartupTests(unittest.TestCase):
    def test_slow_loader_does_not_block_polling(self):
        release=Event();finished=Event();resource=object();disposed=[]
        def factory():
            release.wait(1);finished.set();return resource
        loader=ResourceStartup(factory,disposed.append)
        try:
            for _ in range(100):self.assertEqual(loader.take(),(None,None))
            release.set();self.assertTrue(finished.wait(1));loader.thread.join(1)
            self.assertEqual(loader.take(),(resource,None))
            self.assertEqual(loader.take(),(None,None))
        finally:release.set();loader.close();loader.thread.join(1)
        self.assertEqual(disposed,[])

    def test_exit_before_model_arrives_disposes_it(self):
        release=Event();disposed=Event()
        loader=ResourceStartup(lambda:(release.wait(1),object())[1],lambda value:disposed.set())
        loader.close();release.set();self.assertTrue(disposed.wait(1));loader.thread.join(1)
        self.assertEqual(loader.take(),(None,None))

    def test_completed_unused_model_is_closed_once(self):
        disposed=[];loader=ResourceStartup(lambda:'resource',disposed.append);loader.thread.join(1)
        loader.close();loader.close();self.assertEqual(disposed,['resource'])

    def test_initialization_exception_is_reported(self):
        def factory():raise RuntimeError('synthetic load failure')
        loader=ResourceStartup(factory,lambda value:None);loader.thread.join(1)
        try:self.assertEqual(loader.take(),(None,'RuntimeError: synthetic load failure'))
        finally:loader.close()


if __name__=='__main__':unittest.main()
