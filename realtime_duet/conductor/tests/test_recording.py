"""Synthetic frames only; actual local encoder, bounded queue and decode checks."""
import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch
import cv2
import numpy as np
from conductor_rt.recording import CameraRecording


class RecordingTests(unittest.TestCase):
    def test_real_video_and_frame_timeline_finalize_without_audio_or_landmarks(self):
        with tempfile.TemporaryDirectory() as folder:
            recorder=CameraRecording(folder,queue_size=12)
            for i in range(10):
                recorder.offer(np.full((48,64,3),i*20,dtype=np.uint8),i,100+i/30-.005,100+i/30)
            self.assertTrue(recorder.close(timeout=3))
            report=json.loads((Path(folder)/'recording.json').read_text())
            self.assertIsNone(report['error']);self.assertEqual(report['written'],10)
            self.assertEqual(report['decode']['container_frames'],10)
            self.assertTrue(report['decode']['first_frame']);self.assertTrue(report['decode']['last_frame'])
            self.assertFalse(report['audio']);self.assertGreater(report['playback_duration_s'],0)
            rows=[json.loads(line) for line in (Path(folder)/'camera-frames.jsonl').read_text().splitlines()]
            self.assertEqual([r['capture_frame'] for r in rows],list(range(10)))
            self.assertAlmostEqual(rows[-1]['input_s'],100.3)
            self.assertTrue(all('landmarks' not in r and 'image' not in r for r in rows))

    def test_slow_encoder_drops_frames_without_unbounded_capture_backlog(self):
        original=cv2.VideoWriter
        class Slow:
            def __init__(self,*args):self.writer=original(*args)
            def isOpened(self):return self.writer.isOpened()
            def write(self,image):time.sleep(.03);self.writer.write(image)
            def release(self):self.writer.release()
        with tempfile.TemporaryDirectory() as folder,patch('cv2.VideoWriter',Slow):
            recorder=CameraRecording(folder,queue_size=1)
            begin=time.perf_counter()
            for i in range(100):recorder.offer(np.zeros((48,64,3),np.uint8),i,i/30,i/30)
            elapsed=time.perf_counter()-begin
            self.assertLess(elapsed,.1)
            self.assertTrue(recorder.close(timeout=3))
            self.assertGreater(recorder.dropped,0)
            self.assertLessEqual(recorder.max_queue_depth,1)
            self.assertEqual(recorder.written+recorder.dropped,recorder.offered)
            self.assertIsNone(recorder.final_report['error'])

    def test_stop_recording_refuses_further_frames_but_finalizes_queued_work(self):
        with tempfile.TemporaryDirectory() as folder:
            recorder=CameraRecording(folder)
            recorder.offer(np.zeros((48,64,3),np.uint8),0,1.,1.01)
            recorder.stop()
            recorder.offer(np.zeros((48,64,3),np.uint8),1,1.03,1.04)
            self.assertFalse(recorder.status()['active'])
            self.assertTrue(recorder.close(timeout=3))
            self.assertEqual(recorder.written,1)
            self.assertIsNone(recorder.final_report['error'])
            self.assertTrue(recorder.final_report['decode']['last_frame'])
            self.assertEqual(recorder.final_report['reason'],'operator_recording_stop')

if __name__=='__main__':unittest.main()
