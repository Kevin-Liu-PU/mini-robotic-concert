"""No-camera regressions for explicit recording and Windows status contention."""
import contextlib
import ctypes
from ctypes import wintypes
import io
import json
import os
from pathlib import Path
import tempfile
from threading import Event, Thread
import time
import unittest
from unittest.mock import patch

from conductor.app import parse_args, publish_report
from conductor.recording import LocalRecording, MjpegAviWriter
from tests.test_recording import FakeFrame, FakeWriter


def wait_for(predicate, timeout=2):
    deadline=time.perf_counter()+timeout
    while time.perf_counter()<deadline:
        if predicate():return True
        time.sleep(.005)
    return bool(predicate())


class WindowsReadLock:
    """Real Windows reader that shares reads/writes but denies replacement."""
    def __init__(self,path):
        self.kernel=ctypes.WinDLL('kernel32',use_last_error=True)
        self.kernel.CreateFileW.argtypes=[wintypes.LPCWSTR,wintypes.DWORD,wintypes.DWORD,
                                         wintypes.LPVOID,wintypes.DWORD,wintypes.DWORD,wintypes.HANDLE]
        self.kernel.CreateFileW.restype=wintypes.HANDLE
        self.kernel.CloseHandle.argtypes=[wintypes.HANDLE]
        self.kernel.CloseHandle.restype=wintypes.BOOL
        self.handle=self.kernel.CreateFileW(str(path),0x80000000,3,None,3,0x80,None)
        if self.handle==wintypes.HANDLE(-1).value:raise ctypes.WinError(ctypes.get_last_error())
    def close(self):
        if self.handle is not None:
            self.kernel.CloseHandle(self.handle);self.handle=None
    def __enter__(self):return self
    def __exit__(self,*args):self.close()


class RecordingRepairTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.folder=Path(self.temp.name)/'run';self.now=100.
    def recorder(self,**kwargs):
        kwargs.setdefault('writer_factory',FakeWriter)
        r=LocalRecording(self.folder,90,clock=lambda:self.now,**kwargs)
        def close_quietly():
            with contextlib.redirect_stdout(io.StringIO()):r.close()
        self.addCleanup(close_quietly)
        return r
    def final(self,r):
        return json.loads((self.folder/'final-status.json').read_text())

    def test_preview_and_countdown_do_not_copy_or_save_frames(self):
        r=self.recorder(manual_start=True)
        with patch.object(FakeFrame,'copy',side_effect=AssertionError('Premature frame copy')):
            self.now=400
            self.assertFalse(r.submit(1,self.now,FakeFrame()))
            self.assertFalse(r.request_start(False))
            self.assertEqual(r.status()['state'],'READY')
            self.assertTrue(r.request_start(True))
            self.assertFalse(r.request_start(True))
            self.now=402.999
            self.assertFalse(r.submit(2,self.now,FakeFrame()))
            self.assertGreater(r.status()['countdown_remaining_s'],0)
            self.now=403.05
            self.assertFalse(r.submit(3,402.99,FakeFrame()))
        self.assertTrue(r.submit(4,self.now,FakeFrame()))
        self.assertEqual(r.started,403.05)
        self.assertEqual(r.status()['state'],'RECORDING')
        self.assertEqual(r.status()['remaining_s'],45)
        r.close();self.assertEqual(r.written,1)

    def test_quit_during_ready_or_countdown_never_creates_video(self):
        for countdown in (False,True):
            with self.subTest(countdown=countdown),tempfile.TemporaryDirectory() as folder:
                r=LocalRecording(Path(folder)/'run',0,manual_start=True,writer_factory=FakeWriter)
                if countdown:r.request_start(True)
                self.assertTrue(r.close('quit_key'))
                self.assertEqual(r.status()['state'],'CANCELLED')
                self.assertFalse((r.directory/'camera.avi').exists())
                self.assertFalse(r.request_start(True))

    def test_countdown_is_not_part_of_recording_duration(self):
        r=self.recorder(manual_start=True);r.request_start(True)
        self.now=103;r.submit(1,self.now,FakeFrame())
        self.now=147.999;self.assertTrue(r.submit(2,self.now,FakeFrame()))
        self.now=148;self.assertFalse(r.submit(3,self.now,FakeFrame()))
        self.assertTrue(r.close());self.assertEqual(r.reason,'duration_limit')

    def test_cli_cannot_bypass_manual_start_via_headless(self):
        with contextlib.redirect_stderr(io.StringIO()),self.assertRaises(SystemExit):
            parse_args(['--demo','--record','test','--headless','--seconds','50'])

    def test_final_session_report_failure_retains_data_and_is_explicit(self):
        path=Path(self.temp.name)/'session.json';path.write_text('{"running":true}')
        error=io.StringIO()
        with contextlib.redirect_stderr(error),patch.object(Path,'replace',side_effect=PermissionError('reader lock')):
            self.assertFalse(publish_report(path,dict(running=False,exit_reason='quit_key'),final=True))
        self.assertTrue(json.loads(path.read_text())['running'])
        self.assertFalse(json.loads(path.with_suffix('.json.tmp').read_text())['running'])
        self.assertIn('FINAL SESSION REPORT ERROR',error.getvalue())

    def test_transient_final_session_report_contention_recovers(self):
        path=Path(self.temp.name)/'session.json';original=Path.replace;calls=[]
        def replace(src,dst):
            calls.append(1)
            if len(calls)<3:raise PermissionError('temporary reader lock')
            return original(src,dst)
        with patch.object(Path,'replace',replace):
            self.assertTrue(publish_report(path,dict(running=False),final=True))
        self.assertEqual(len(calls),3);self.assertFalse(json.loads(path.read_text())['running'])

    def test_transient_metadata_failure_does_not_stop_evidence(self):
        blocked=Event();blocked.set();attempted=Event()
        def replace(src,dst):
            attempted.set()
            if blocked.is_set():raise PermissionError('simulated Windows reader')
            src.replace(dst)
        with contextlib.redirect_stdout(io.StringIO()):
            r=self.recorder(manifest_replace=replace)
            self.assertTrue(attempted.wait(1))
            self.assertTrue(wait_for(lambda:r.metadata_warning))
            for i in range(5):
                self.now=100+i*.05;r.submit(i+1,self.now,FakeFrame())
                r.add_diagnostic(dict(sequence=i+1,capture_monotonic_s=self.now))
                self.assertTrue(wait_for(lambda:r.written==i+1))
            self.assertFalse(r.stop_event.is_set());self.assertIsNone(r.error)
            blocked.clear()
            self.assertTrue(wait_for(lambda:r.metadata_recoveries>=1))
            self.assertTrue(r.close())
        s=self.final(r)
        self.assertEqual(s['written_frames'],5);self.assertEqual(s['diagnostic_written'],5)
        self.assertTrue(s['finalized']);self.assertIsNone(s['metadata_warning'])
        self.assertGreater(s['metadata_retries'],0)

    def test_persistent_final_metadata_failure_is_error_with_complete_evidence(self):
        def replace(src,dst):raise PermissionError('persistent manifest reader')
        with contextlib.redirect_stdout(io.StringIO()):
            r=self.recorder(manifest_replace=replace)
            r.submit(1,100,FakeFrame());r.add_diagnostic(dict(sequence=1,capture_monotonic_s=100))
            self.assertFalse(r.close())
        s=self.final(r)
        self.assertEqual(s['state'],'ERROR');self.assertFalse(s['finalized'])
        self.assertTrue(s['evidence_finalized']);self.assertFalse(s['metadata_finalized'])
        self.assertIn('Final metadata publication failed',s['error'])
        self.assertEqual(s['written_frames'],1);self.assertEqual(s['diagnostic_written'],1)
        self.assertFalse(r.worker.is_alive());self.assertFalse(r.publisher.is_alive())

    def test_nonsharing_metadata_disk_error_is_visible_and_not_hot_retried(self):
        def replace(src,dst):raise OSError('simulated metadata disk error')
        with contextlib.redirect_stdout(io.StringIO()):
            r=self.recorder(manifest_replace=replace)
            self.assertTrue(wait_for(lambda:r.metadata_failures>=1))
            r.submit(1,100,FakeFrame());self.assertFalse(r.close())
        self.assertEqual(r.metadata_retries,0)
        self.assertEqual(r.written,1);self.assertEqual(r.status()['state'],'ERROR')

    def test_failed_video_queue_has_honest_final_accounting(self):
        entered,release=Event(),Event()
        class Broken(FakeWriter):
            def write(self,frame):
                entered.set();release.wait(2);raise OSError('video disk error')
        with contextlib.redirect_stdout(io.StringIO()):
            r=self.recorder(writer_factory=Broken)
            r.submit(1,100,FakeFrame());self.assertTrue(entered.wait(1))
            for i in range(2,5):
                self.now=100+i*.01;r.submit(i,self.now,FakeFrame())
                r.add_diagnostic(dict(sequence=i,capture_monotonic_s=self.now))
            release.set();self.assertFalse(r.close())
        s=self.final(r)
        self.assertEqual(s['submitted_frames'],s['written_frames']+s['failed_frame_records'])
        self.assertEqual(s['submitted_frames'],s['encoded_frames']+s['unwritten_video_frames'])
        self.assertEqual(s['diagnostic_received'],s['diagnostic_written']+s['diagnostic_drops'])
        self.assertEqual(s['pending_frames'],0);self.assertEqual(s['pending_diagnostics'],0)
        self.assertFalse(s['evidence_finalized']);self.assertEqual(s['state'],'ERROR')

    def test_mapping_failure_counts_encoded_but_unmapped_video(self):
        real_line=LocalRecording._line
        def line(stream,value):
            if str(stream.name).endswith('video.frames.jsonl'):raise OSError('mapping disk error')
            real_line(stream,value)
        with contextlib.redirect_stdout(io.StringIO()),patch.object(LocalRecording,'_line',staticmethod(line)):
            r=self.recorder();r.submit(1,100,FakeFrame());self.assertFalse(r.close())
        s=self.final(r)
        self.assertEqual(s['encoded_frames'],1);self.assertEqual(s['written_frames'],0)
        self.assertEqual(s['unmapped_video_frames'],1);self.assertFalse(s['evidence_finalized'])

    def test_diagnostic_disk_failure_remains_fatal(self):
        real_line=LocalRecording._line
        def line(stream,value):
            if Path(stream.name).name=='diagnostics.jsonl':raise OSError('diagnostic disk error')
            real_line(stream,value)
        with contextlib.redirect_stdout(io.StringIO()),patch.object(LocalRecording,'_line',staticmethod(line)):
            r=self.recorder();r.submit(1,100,FakeFrame())
            r.add_diagnostic(dict(sequence=1,capture_monotonic_s=100));self.assertFalse(r.close())
        s=self.final(r)
        self.assertEqual(s['diagnostic_drops'],1);self.assertFalse(s['evidence_finalized'])
        self.assertIn('diagnostic disk error',s['error'])

    def test_final_summary_disk_failure_refreshes_manifest_as_error(self):
        real_line=LocalRecording._line
        def line(stream,value):
            if Path(stream.name).name=='final-status.json':raise OSError('final summary disk error')
            real_line(stream,value)
        with contextlib.redirect_stdout(io.StringIO()),patch.object(LocalRecording,'_line',staticmethod(line)):
            r=self.recorder();r.submit(1,100,FakeFrame());self.assertFalse(r.close())
        s=json.loads((self.folder/'manifest.json').read_text())
        self.assertEqual(s['state'],'ERROR');self.assertFalse(s['final_status_saved'])
        self.assertFalse(s['finalized']);self.assertIn('final summary disk error',s['error'])

    def test_combined_video_error_and_later_manifest_failure_keep_final_summary_truthful(self):
        class Broken(FakeWriter):
            def write(self,frame):raise OSError('simulated video failure')
        final_attempts=[]
        def replace(src,dst):
            payload=json.loads(src.read_text())
            if payload['metadata_finalized']:
                final_attempts.append(1)
                if len(final_attempts)>1:raise PermissionError('late manifest contention')
            src.replace(dst)
        with contextlib.redirect_stdout(io.StringIO()):
            r=self.recorder(writer_factory=Broken,manifest_replace=replace)
            r.submit(1,100,FakeFrame());self.assertTrue(r.finished.wait(2))
            first=self.final(r)
            self.assertEqual(len(final_attempts),1)
            self.assertEqual(first['state'],'ERROR')
            self.assertTrue(first['metadata_finalized']);self.assertEqual(first['metadata_failures'],0)
            self.assertFalse(r.close())
            saved=self.final(r);status=r.status()
        for key in ('state','metadata_finalized','metadata_failures','metadata_warning','error','finalized'):
            self.assertEqual(saved[key],status[key],key)
        self.assertFalse(saved['metadata_finalized']);self.assertEqual(saved['metadata_failures'],4)

    def test_blocked_source_callback_cannot_claim_clean_shutdown(self):
        entered,release=Event(),Event()
        def packet():entered.set();release.wait(5);return None
        with contextlib.redirect_stdout(io.StringIO()):
            r=self.recorder();r.start_source(packet);self.assertTrue(entered.wait(1))
            try:
                self.assertFalse(r.close())
                self.assertEqual(r.status()['state'],'ERROR')
                self.assertIn('source thread did not stop',r.error)
                self.assertFalse(r.status()['finalized'])
            finally:release.set();r.producer.join(1)
        self.assertFalse(r.producer.is_alive())

    def test_second_thread_start_failure_cleans_up_the_writer(self):
        original_start=Thread.start;started=[]
        def start(thread):
            if thread.name=='conductor-status-publisher':raise RuntimeError('simulated thread limit')
            started.append(thread);return original_start(thread)
        with contextlib.redirect_stdout(io.StringIO()),patch.object(Thread,'start',start),self.assertRaises(RuntimeError):
            LocalRecording(self.folder,90,manual_start=True,writer_factory=FakeWriter)
        self.assertTrue(started);self.assertFalse(any(t.is_alive() for t in started))

    def test_avi_header_write_failure_closes_the_file(self):
        class ShortFile(io.BytesIO):
            def write(self,value):return 0
        stream=ShortFile()
        with patch.object(Path,'open',return_value=stream),self.assertRaises(OSError):
            MjpegAviWriter(self.folder/'camera.avi',16,12)
        self.assertTrue(stream.closed)

    def test_app_c_countdown_and_q_states_without_camera_or_native_window(self):
        import cv2
        from conductor.app import main
        for quit_early in (True,False):
            with self.subTest(quit_during_countdown=quit_early),tempfile.TemporaryDirectory() as folder:
                now=[100.];recorders=[];states=[]
                report=Path(folder)/'session.json'
                def factory(directory,origin,**kwargs):
                    r=LocalRecording(Path(folder)/'recording',origin,clock=lambda:now[0],writer_factory=FakeWriter,**kwargs)
                    recorders.append(r);return r
                def key(_):
                    state=recorders[0].status()['state'];states.append(state)
                    if len(states)==1:return ord('C')
                    if len(states)==2:
                        if quit_early:return ord('Q')
                        now[0]=101.;return -1
                    if len(states)==3:now[0]=102.;return -1
                    if len(states)==4:now[0]=103.1;return -1
                    return ord('q')
                with contextlib.redirect_stdout(io.StringIO()),patch('sys.argv',[
                        'conductor.app','--demo','--record','manual-test','--report',str(report)]),\
                     patch('conductor.recording.LocalRecording',side_effect=factory),\
                     patch('conductor.capture.Camera',side_effect=AssertionError('Camera forbidden')),\
                     patch('conductor.app.time.perf_counter',side_effect=lambda:now[0]),\
                     patch('conductor.app.time.perf_counter_ns',side_effect=lambda:int(now[0]*1e9)),\
                     patch.object(cv2,'namedWindow'),patch.object(cv2,'resizeWindow'),\
                     patch.object(cv2,'imshow'),patch.object(cv2,'destroyAllWindows'),\
                     patch.object(cv2,'getWindowProperty',return_value=1),\
                     patch.object(cv2,'waitKey',side_effect=key):
                    self.assertEqual(main(),0)
                self.assertEqual(states[:2],['READY','COUNTDOWN'])
                s=json.loads(report.read_text())['recording']
                self.assertEqual(s['written_frames'],0 if quit_early else 1)
                self.assertEqual(s['state'],'CANCELLED' if quit_early else 'CLOSED')
                if not quit_early:
                    self.assertEqual(states[-1],'RECORDING')
                    self.assertGreaterEqual(s['started_monotonic_s'],103)

    @unittest.skipUnless(os.name=='nt','Windows sharing mode regression')
    def test_actual_windows_read_handle_does_not_abort_writer(self):
        with contextlib.redirect_stdout(io.StringIO()):
            r=self.recorder()
            self.assertTrue(wait_for(lambda:(self.folder/'manifest.json').exists()))
            with WindowsReadLock(self.folder/'manifest.json'):
                self.assertTrue(wait_for(lambda:r.metadata_warning))
                self.assertIn('WinError',r.metadata_warning)
                for i in range(3):
                    self.now=100+i*.03;r.submit(i+1,self.now,FakeFrame())
                    self.assertTrue(wait_for(lambda:r.written==i+1))
                self.assertIsNone(r.error);self.assertFalse(r.stop_event.is_set())
            self.assertTrue(wait_for(lambda:r.metadata_recoveries>0))
            self.assertTrue(r.close())
        self.assertEqual(r.written,3);self.assertTrue(self.final(r)['finalized'])


if __name__=='__main__':unittest.main()
