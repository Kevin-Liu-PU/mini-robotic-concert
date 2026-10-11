"""Synthetic-only recording lifecycle, bounded overload and in-memory AVI/JPEG serialization."""
import contextlib
import io
import json
from pathlib import Path
import tempfile
from threading import Event
import time
import unittest
from unittest.mock import patch

from conductor.app import parse_args
from conductor.recording import LocalRecording, MjpegAviWriter
from conductor.recording_diagnostics import detector_state, diagnostic_record
from conductor.multitrack import ConductorSession
from tests.test_tip_anchor import moving,observation


class FakeFrame:
    shape=(12,16,3)
    def copy(self):return self


class FakeWriter:
    def __init__(self,*args):self.count=0
    def write(self,frame):self.count+=1
    def release(self):pass


class RecordingTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.folder=Path(self.temp.name)/'run';self.now=100.0

    def recorder(self,**kwargs):
        r=LocalRecording(self.folder,90,clock=lambda:self.now,writer_factory=FakeWriter,**kwargs)
        self.addCleanup(r.close)
        return r

    def test_no_cli_recording_by_default(self):
        self.assertIsNone(parse_args([]).record)

    def test_cli_limits_duration_and_forbids_paths_or_implicit_recording(self):
        for args in (['--record','../secret'],['--record','C:\\temp'],['--record','\\\\server'],
                     ['--record','test','--record-seconds','46'],['--record','test','--record-seconds','nan'],
                     ['--record-seconds','5'],['--record','test','--list-cameras']):
            with self.subTest(args=args),contextlib.redirect_stderr(io.StringIO()),self.assertRaises(SystemExit):parse_args(args)
        self.assertEqual(parse_args(['--record','missed-beats']).record_seconds,45)

    def test_early_quit_flushes_and_keeps_real_timestamp_mapping(self):
        r=self.recorder()
        for i,t in enumerate((100,100.07,100.13)):
            self.now=t;r.submit(i+1,t,FakeFrame())
        self.assertTrue(r.close('quit_key'))
        rows=[json.loads(x) for x in (self.folder/'video.frames.jsonl').read_text().splitlines()]
        self.assertEqual([x['sequence'] for x in rows],[1,2,3])
        self.assertEqual([x['capture_monotonic_s'] for x in rows],[100,100.07,100.13])
        self.assertNotEqual(rows[1]['recording_time_s'],rows[1]['playback_time_s'])
        s=json.loads((self.folder/'manifest.json').read_text())
        self.assertTrue(s['finalized']);self.assertEqual(s['reason'],'quit_key')
        self.assertFalse(s['audio']);self.assertFalse(s['uploaded'])

    def test_45_second_boundary_excludes_boundary_and_later_frames(self):
        r=self.recorder()
        self.assertTrue(r.submit(1,100,FakeFrame()))
        self.now=144.9;self.assertTrue(r.submit(2,self.now,FakeFrame()))
        self.now=145;self.assertFalse(r.submit(3,self.now,FakeFrame()))
        self.assertTrue(r.close())
        self.assertEqual(r.status()['written_frames'],2)
        self.assertEqual(r.status()['reason'],'duration_limit')

    def test_deadline_stops_even_if_source_stalls(self):
        r=self.recorder();r.submit(1,100,FakeFrame());self.now=146
        self.assertTrue(r.stop_event.wait(1))
        self.assertEqual(r.status()['reason'],'duration_limit')

    def test_no_frame_quit_finalizes_metadata_without_fabricated_video(self):
        r=self.recorder();self.assertTrue(r.close())
        self.assertFalse((self.folder/'camera.avi').exists())
        self.assertEqual(r.status()['written_frames'],0)

    def test_existing_run_never_overwritten(self):
        self.folder.mkdir();(self.folder/'keep').write_text('original')
        with self.assertRaises(FileExistsError):self.recorder()
        self.assertEqual((self.folder/'keep').read_text(),'original')

    def test_slow_encoder_has_bounded_queue_and_explicit_drops(self):
        entered,release=Event(),Event()
        class Slow(FakeWriter):
            def write(self,frame):entered.set();release.wait(3)
        r=LocalRecording(self.folder,90,clock=lambda:self.now,queue_size=2,writer_factory=Slow)
        self.addCleanup(release.set);self.addCleanup(r.close)
        r.submit(1,100,FakeFrame());self.assertTrue(entered.wait(1))
        began=time.perf_counter()
        for i in range(2,15):self.now=100+i*.01;r.submit(i,self.now,FakeFrame())
        elapsed=time.perf_counter()-began
        self.assertLess(elapsed,.2);self.assertLessEqual(r.frames.qsize(),2)
        self.assertGreater(r.status()['dropped_frames'],0)
        release.set();self.assertTrue(r.close())
        issues=[json.loads(x) for x in (self.folder/'recording.issues.jsonl').read_text().splitlines()]
        self.assertTrue(any(x['reason']=='video_queue_full' for x in issues))

    def test_source_sequence_gaps_are_reported(self):
        r=self.recorder();r.submit(4,100,FakeFrame());self.now=100.2;r.submit(7,self.now,FakeFrame());r.close()
        self.assertEqual(r.status()['dropped_frames'],2)
        self.assertIn('source_sequence_gap',(self.folder/'recording.issues.jsonl').read_text())

    def test_duplicate_or_old_source_timestamp_never_creates_extra_frame(self):
        r=self.recorder();r.submit(1,100,FakeFrame())
        self.assertFalse(r.submit(1,100,FakeFrame()));self.assertFalse(r.submit(2,99,FakeFrame()))
        r.close();self.assertEqual(r.status()['written_frames'],1)

    def test_writer_failure_is_visible_and_does_not_change_detection(self):
        class Broken(FakeWriter):
            def write(self,frame):raise OSError('simulated disk failure')
        with contextlib.redirect_stdout(io.StringIO()):
            r=LocalRecording(self.folder,90,clock=lambda:self.now,writer_factory=Broken)
            r.submit(1,100,FakeFrame());self.assertTrue(r.finished.wait(1))
            self.assertFalse(r.close())
        s=json.loads((self.folder/'manifest.json').read_text())
        self.assertEqual(s['state'],'ERROR');self.assertIn('disk failure',s['error']);self.assertFalse(s['finalized'])

    def test_release_failure_is_not_reported_as_saved(self):
        class Broken(FakeWriter):
            def release(self):raise OSError('simulated flush failure')
        with contextlib.redirect_stdout(io.StringIO()):
            r=LocalRecording(self.folder,90,clock=lambda:self.now,writer_factory=Broken)
            r.submit(1,100,FakeFrame());self.assertFalse(r.close())
        self.assertEqual(r.status()['state'],'ERROR')

    def test_last_inflight_diagnostic_has_a_grace_window(self):
        r=self.recorder();r.submit(1,100,FakeFrame());self.now=145.01
        self.assertTrue(r.stop_event.wait(1))
        self.assertTrue(r.add_diagnostic(dict(sequence=2,capture_monotonic_s=144.99)))
        r.close()
        self.assertIn('144.99',(self.folder/'diagnostics.jsonl').read_text())

    def test_very_late_diagnostic_is_explicit_in_final_journal_and_count(self):
        r=self.recorder();r.submit(1,100,FakeFrame());r.close()
        self.assertFalse(r.add_diagnostic(dict(sequence=1,capture_monotonic_s=100)))
        r.close()
        self.assertEqual(json.loads((self.folder/'manifest.json').read_text())['diagnostic_drops'],1)
        self.assertIn('late_diagnostic_after_finalize',(self.folder/'recording.issues.jsonl').read_text())

    def test_diagnostic_queue_and_issue_details_are_bounded_and_overflow_counted(self):
        entered,release=Event(),Event()
        class Slow(FakeWriter):
            def write(self,frame):entered.set();release.wait(3)
        r=LocalRecording(self.folder,90,clock=lambda:self.now,writer_factory=Slow)
        r.submit(1,100,FakeFrame());self.assertTrue(entered.wait(1))
        for i in range(600):r.add_diagnostic(dict(sequence=i,capture_monotonic_s=100))
        self.assertLessEqual(r.diagnostics.qsize(),256)
        self.assertGreater(r.status()['diagnostic_drops'],0);self.assertGreater(r.status()['issue_details_overflow'],0)
        release.set();r.close()

    def test_source_pump_samples_unique_fresh_packets_without_opening_device(self):
        r=self.recorder();packet=[(1,100,FakeFrame())]
        r.start_source(lambda:packet[0])
        deadline=time.perf_counter()+1
        while r.status()['submitted_frames']<1 and time.perf_counter()<deadline:time.sleep(.005)
        self.now=100.1;packet[0]=(2,self.now,FakeFrame())
        deadline=time.perf_counter()+1
        while r.status()['submitted_frames']<2 and time.perf_counter()<deadline:time.sleep(.005)
        r.close();self.assertEqual(r.status()['written_frames'],2);self.assertFalse(r.producer.is_alive())

    def test_diagnostics_observe_but_do_not_change_beat_timestamps(self):
        baseline,recorded=ConductorSession(),ConductorSession();a=[];b=[]
        for i in range(300):
            t=i/30;before=detector_state(recorded.engine)
            ea=baseline.update([moving(t)],t);eb=recorded.update([moving(t)],t)
            row=diagnostic_record(recorded,[moving(t)],i,t,0,before)
            json.dumps(row,allow_nan=False)
            if ea:a.append(ea.t)
            if eb:b.append(eb.t)
        self.assertEqual(a,b);self.assertEqual(baseline.engine.diagnostic_counts,recorded.engine.diagnostic_counts)

    def test_diagnostics_include_invalid_tip_and_exact_reset_reason(self):
        s=ConductorSession()
        for i in range(40):s.update([moving(i/30)],i/30)
        before=detector_state(s.engine);h=observation();h.landmarks[8].y=float('nan')
        s.update([h],2);row=diagnostic_record(s,[h],60,2,0,before)
        self.assertFalse(row['candidates'][0]['tip_valid'])
        self.assertIsNone(row['candidates'][0]['tip8']['y'])
        self.assertTrue(any(e['event']=='reset' for e in row['events']))
        json.dumps(row,allow_nan=False)

    def test_diagnostics_identify_unarmed_crossing_and_tip_jump_with_stable_wrist(self):
        s=ConductorSession()
        for i in range(40):s.update([observation(.58)],i/30)
        h=observation(.62);before=detector_state(s.engine)
        s.update([h],40/30);row=diagnostic_record(s,[h],40,40/30,0,before)
        self.assertTrue(any(e['reason']=='not_armed' for e in row['events']))
        self.assertFalse(row['detector_before']['armed']);token=s.primary_token
        h=observation(.62);h.landmarks[8].x=.8;before=detector_state(s.engine)
        s.update([h],41/30);row=diagnostic_record(s,[h],41,41/30,0,before)
        self.assertEqual(s.primary_token,token)
        self.assertTrue(any(e['reason']=='position_jump' for e in row['events']))
        self.assertGreater(row['motion']['observed_tip_step'],row['motion']['step_limit'])


try:
    import cv2
    import numpy as np
except ImportError:cv2=None


@unittest.skipIf(cv2 is None,'OpenCV needed for synthetic video interoperability')
class AviTests(unittest.TestCase):
    def test_checked_mjpeg_avi_decodes_in_order_without_audio_stream(self):
        import struct
        class Buffer(io.BytesIO):
            def fileno(self): return -1
            def close(self): self.saved = self.getvalue(); super().close()
        stream = Buffer()
        with patch.object(Path, 'open', return_value=stream), patch('conductor.recording.os.fsync'):
            writer = MjpegAviWriter(Path('synthetic.avi'),160,90)
            for i in range(8): writer.write(np.full((90,160,3),i*25,np.uint8))
            writer.release()
        data = stream.saved
        self.assertEqual(data[:4], b'RIFF')
        self.assertEqual(struct.unpack_from('<I', data, 4)[0],len(data)-8)
        start = data.index(b'movi')+4; means=[]
        while data[start:start+4] == b'00dc':
            size=struct.unpack_from('<I',data,start+4)[0]
            frame=cv2.imdecode(np.frombuffer(data[start+8:start+8+size],dtype=np.uint8),cv2.IMREAD_COLOR)
            means.append(float(frame.mean())); start += 8+size+(size%2)
        self.assertEqual(data[start:start+4], b'idx1')
        self.assertEqual(struct.unpack_from('<I',data,start+4)[0],8*16)
        self.assertEqual(len(means),8)
        self.assertTrue(all(abs(v-i*25)<3 for i,v in enumerate(means)))
        self.assertNotIn(b'auds',data[:256])

    def test_dimension_change_is_an_explicit_error(self):
        with tempfile.TemporaryDirectory() as d:
            with patch.object(Path, 'open', return_value=io.BytesIO()):
                writer=MjpegAviWriter(Path(d)/'test.avi',160,90)
            self.addCleanup(writer.file.close)
            writer.file.fileno = lambda: -1
            with self.assertRaises(ValueError):writer.write(np.zeros((20,30,3),np.uint8))
            with patch('conductor.recording.os.fsync'):
                writer.release()

    def test_app_q_handler_finalizes_recording_without_camera_or_native_window(self):
        from conductor.app import main
        with tempfile.TemporaryDirectory() as d:
            folder=Path(d)/'recording';report=Path(d)/'session.json'
            actual=LocalRecording
            def factory(directory,origin,**kwargs):return actual(folder,origin,writer_factory=FakeWriter,**kwargs)
            with contextlib.redirect_stdout(io.StringIO()),patch('sys.argv',[
                    'conductor.app','--demo','--record','unit-test-q','--report',str(report)]),\
                 patch('conductor.recording.LocalRecording',side_effect=factory),\
                 patch('conductor.capture.Camera',side_effect=AssertionError('Camera forbidden')),\
                 patch.object(cv2,'namedWindow'),patch.object(cv2,'resizeWindow'),\
                 patch.object(cv2,'imshow'),patch.object(cv2,'destroyAllWindows'),\
                 patch.object(cv2,'waitKey',return_value=ord('q')):
                self.assertEqual(main(),0)
            saved=json.loads(report.read_text())
            self.assertEqual(saved['exit_reason'],'quit_key')
            self.assertTrue(saved['recording_closed_cleanly'])
            self.assertEqual(saved['recording']['reason'],'quit_key')
            self.assertEqual(saved['recording']['written_frames'],0)
            self.assertEqual(saved['recording']['state'],'CANCELLED')
            self.assertFalse((folder/'camera.avi').exists())

    def test_rec_banner_and_drop_warning_are_visible_in_synthetic_render(self):
        from tests.test_tip_display import TipDisplayTests
        scene=TipDisplayTests();session,info=scene.scene()
        info['recording']=dict(state='RECORDING',remaining_s=34.2,dropped_frames=2,diagnostic_drops=1)
        with patch.object(cv2,'putText',wraps=cv2.putText) as labels:
            scene.draw(session,info,np.zeros((540,960,3),np.uint8))
        strings=[c.args[1] for c in labels.call_args_list]
        self.assertIn('REC 34.2s  |  NO AUDIO',strings)
        self.assertIn('REC LOSS: 2 frames / 1 diagnostics',strings)


if __name__=='__main__':unittest.main()
