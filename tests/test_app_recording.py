"""App-view recording with fake camera/model/window and an in-memory writer."""
import contextlib
import io
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import cv2
import numpy as np

from conductor import app
from conductor.recording import LocalRecording
from tests.test_recording import FakeWriter


class AppRecordingTests(unittest.TestCase):
    def test_app_recording_requires_explicit_record_and_retains_manual_limits(self):
        self.assertEqual(app.parse_args([]).record_view, 'camera')
        for argv in (['--record-view', 'app'],
                     ['--record', 'test', '--record-view', 'app', '--headless'],
                     ['--record', 'test', '--record-view', 'app', '--record-seconds', '46']):
            with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                app.parse_args(argv)
        args = app.parse_args(['--record', 'test', '--record-view', 'app'])
        self.assertEqual(args.record_seconds, 45)
        self.assertFalse(args.mock_cello or args.audio_clock)

    def test_canvas_and_clock_are_paired_with_source_timestamp_without_mutation(self):
        recording, canvas = Mock(), object()
        diagnostic = dict(sequence=42, capture_monotonic_s=12.3, sample_time_s=2.3)
        state = dict(state='HOLDING_LAST_TEMPO', predicted_count=8)
        app.record_rendered_frame(recording, canvas, diagnostic, 12.35, state)
        recording.submit.assert_called_once_with(42, 12.3, canvas)
        row = recording.add_diagnostic.call_args.args[0]
        self.assertEqual(row['rendered_monotonic_s'], 12.35)
        self.assertEqual(row['screen_clock'], state)
        state['predicted_count'] = 9
        self.assertEqual(row['screen_clock']['predicted_count'], 8)
        self.assertNotIn('rendered_monotonic_s', diagnostic)
        recording.reset_mock()
        app.record_rendered_frame(recording, canvas, None, 13)
        recording.submit.assert_not_called()
        recording.add_diagnostic.assert_not_called()

    def test_live_app_branch_records_display_canvas_and_never_starts_raw_pump(self):
        for view in ('app', 'camera'):
            with self.subTest(view=view), tempfile.TemporaryDirectory() as directory, contextlib.ExitStack() as stack:
                now, sequence = [100.], [0]
                frame = np.zeros((90, 160, 3), np.uint8)
                canvas = np.full((120, 220, 3), 17, np.uint8)
                # Distinct UI overlay proves the recorded object is the final canvas.
                overlay = canvas.copy(); overlay[:10] = 99
                def latest():
                    now[0] += .05
                    sequence[0] += 1
                    return sequence[0], now[0], frame
                camera = SimpleNamespace(latest=latest, close=Mock(return_value=True),
                    stats=lambda: dict(worker_alive=True, status='CAMERA STREAMING - FRESH FRAMES',
                                       fresh_frame_age_s=0, capture_fps_recent=20, mode='synthetic'))
                model = SimpleNamespace(detect_for_video=lambda *a: SimpleNamespace(hand_landmarks=[], handedness=[]),
                                        close=Mock())
                mp = SimpleNamespace(Image=lambda **k: object(), ImageFormat=SimpleNamespace(SRGB=1))
                pending = [(mp, model)]
                startup = SimpleNamespace(take=lambda: (pending.pop() if pending else None, None), close=Mock())
                recordings = []
                def recorder(directory_arg, origin, **kwargs):
                    r = LocalRecording(Path(directory)/'recording', origin,
                        clock=lambda: now[0], writer_factory=FakeWriter, **kwargs)
                    r.start_source = Mock()  # Raw path contract only; never run a pump.
                    r.submit = Mock(wraps=r.submit)
                    recordings.append(r)
                    return r
                report_path = Path(directory)/'session.json'
                argv = ['conductor.app', '--config', str(Path(__file__).resolve().parents[1]/'config-responsive.json'),
                        '--hold-clock', '--backend', 'avfoundation', '--camera-built-in',
                        '--record', 'unit-app-view', '--record-view', view, '--record-seconds', '1',
                        '--seconds', '4.5', '--report', str(report_path)]
                stack.enter_context(patch.object(sys, 'argv', argv))
                stack.enter_context(patch.object(app.time, 'perf_counter', side_effect=lambda: now[0]))
                stack.enter_context(patch.object(app.time, 'perf_counter_ns', side_effect=lambda: round(now[0]*1e9)))
                stack.enter_context(patch('conductor.devices.resolve_camera', side_effect=lambda cfg, *a, **k: dict(cfg, camera_name='Synthetic')))
                stack.enter_context(patch('conductor.capture.Camera', return_value=camera))
                stack.enter_context(patch('conductor.startup.ResourceStartup', return_value=startup))
                stack.enter_context(patch('conductor.recording.LocalRecording', side_effect=recorder))
                stack.enter_context(patch('conductor.display.render', return_value=canvas))
                stack.enter_context(patch('conductor.screen_display.render_clock', return_value=overlay))
                for name in ('namedWindow', 'resizeWindow', 'destroyAllWindows'):
                    stack.enter_context(patch.object(cv2, name))
                shown = stack.enter_context(patch.object(cv2, 'imshow'))
                stack.enter_context(patch.object(cv2, 'getWindowProperty', return_value=1))
                stack.enter_context(patch.object(cv2, 'waitKey', side_effect=lambda _: ord('c') if sequence[0] == 1 else 255))
                stack.enter_context(contextlib.redirect_stdout(io.StringIO()))
                self.assertEqual(app.main(), 0)
                r = recordings[0]
                saved = json.loads(report_path.read_text())
                self.assertIsNone(saved['mock_cello'])
                self.assertFalse(saved['audio_clock']['enabled'])
                self.assertEqual(saved['screen_clock_policy'], 'responsive_reentry_phase_v2')
                if view == 'app':
                    r.start_source.assert_not_called()
                    self.assertGreater(saved['recording']['written_frames'], 0)
                    self.assertEqual(saved['recording']['scope'], 'rendered_app_processed_frames')
                    self.assertTrue(all(call.args[2] is overlay for call in r.submit.call_args_list))
                    self.assertTrue(all(call.args[1] is overlay for call in shown.call_args_list))
                    rows = [json.loads(line) for line in (r.directory/'diagnostics.jsonl').read_text().splitlines()]
                    self.assertTrue(rows)
                    self.assertTrue(all(row['rendered_monotonic_s'] >= row['capture_monotonic_s'] for row in rows))
                    self.assertTrue(all(row['screen_clock']['follow_policy'] == 'responsive_reentry_phase_v2' for row in rows))
                    mapping = [json.loads(line) for line in (r.directory/'video.frames.jsonl').read_text().splitlines()]
                    indexed = {row['sequence']: row for row in rows}
                    self.assertTrue(all(row['capture_monotonic_s'] == indexed[row['sequence']]['capture_monotonic_s'] for row in mapping))
                else:
                    r.start_source.assert_called_once_with(camera.latest)
                    r.submit.assert_not_called()
                    self.assertEqual(saved['recording']['scope'], 'source_camera_unmirrored')


if __name__ == '__main__':
    unittest.main()
