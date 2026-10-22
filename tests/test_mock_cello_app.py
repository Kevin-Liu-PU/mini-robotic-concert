"""Execute app.main with fake time, drawing and devices; stdlib only."""
import contextlib
import io
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from conductor import app
from conductor.mock_cello import MockCelloControls


class FakeTime:
    def __init__(self):
        self.now = 100.

    def perf_counter(self):
        return self.perf_counter_ns() / 1e9

    def perf_counter_ns(self):
        return round(self.now * 1e9)

    def sleep(self, seconds):
        self.now += seconds


class AppLoopTests(unittest.TestCase):
    def run_app(self, *, enabled=True, hold=False, keys=None, render_error=False):
        clock = FakeTime()
        mock = MockCelloControls()
        keys = dict(keys or {31: ord('a'), 32: ord('b')})
        polls = 0

        def key():
            nonlocal polls
            polls += 1
            return keys.get(polls, 255)

        def forbidden(*args, **kwargs):
            raise AssertionError('Unexpected device access')

        cv2 = SimpleNamespace(WINDOW_NORMAL=0, WND_PROP_VISIBLE=0,
            namedWindow=Mock(), resizeWindow=Mock(), imshow=Mock(), waitKey=lambda _: key(),
            getWindowProperty=lambda *_: 1, destroyAllWindows=Mock(),
            VideoCapture=forbidden, VideoWriter=forbidden)
        modules = dict(cv2=cv2, numpy=SimpleNamespace(full=lambda *_: 'synthetic frame', uint8=int),
            sounddevice=SimpleNamespace(OutputStream=forbidden, InputStream=forbidden),
            **{'conductor.capture': SimpleNamespace(Camera=forbidden, rate=lambda values: len(values)),
               'conductor.display': SimpleNamespace(render=Mock(return_value='fake canvas')),
               'conductor.screen_display': SimpleNamespace(render_clock=lambda canvas, *a, **k: canvas)})
        output = io.StringIO()
        with tempfile.TemporaryDirectory() as directory, contextlib.ExitStack() as stack:
            args = ['conductor.app', '--demo', '--seconds', '4', '--report', str(Path(directory)/'session.json')]
            if enabled:
                args.append('--mock-cello')
            if hold:
                args.append('--hold-clock')
            stack.enter_context(patch.dict(sys.modules, modules))
            stack.enter_context(patch.object(sys, 'argv', args))
            stack.enter_context(patch.object(app, 'time', clock))
            stack.enter_context(patch('conductor.mock_cello.MockCelloControls', return_value=mock))
            strip = stack.enter_context(patch.object(app, 'render_mock_cello', return_value='mock canvas',
                side_effect=RuntimeError('injected render failure') if render_error else None))
            offered = stack.enter_context(patch.object(mock.bow, 'offer', wraps=mock.bow.offer))
            stack.enter_context(contextlib.redirect_stdout(output))
            if render_error:
                with self.assertRaisesRegex(RuntimeError, 'injected render failure'):
                    app.main()
                return mock
            self.assertEqual(app.main(), 0)
            report = json.loads(output.getvalue())
            if enabled:
                self.assertGreater(strip.call_count, 0)
                self.assertEqual(offered.call_count, report['detected_strokes'])
            else:
                strip.assert_not_called()
                offered.assert_not_called()
        return report

    def test_main_observed_beats_drive_exactly_one_mock_bow(self):
        report = self.run_app()
        self.assertEqual((report['mock_cello']['started'], report['mock_cello']['completed']), (1, 1))
        self.assertEqual(report['mock_cello']['state'], 'DISARMED')
        self.assertEqual(report['mock_cello']['reason'], 'APPLICATION_EXIT')

    def test_hold_clock_behavior_unchanged_with_mock(self):
        baseline = self.run_app(enabled=False, hold=True)
        enabled = self.run_app(hold=True)
        for key in ('screen_clock', 'audio_clock', 'detected_strokes', 'final_bpm'):
            self.assertEqual(baseline[key], enabled[key], key)
        self.assertIsNone(baseline['mock_cello'])

    def test_main_enter_clears_authorization_before_next_observed_beat(self):
        report = self.run_app(hold=True, keys={31: ord('a'), 32: ord('b'), 33: 13})
        self.assertEqual(report['mock_cello']['started'], 0)

    def test_application_exception_latches_mock_fault_through_finally(self):
        mock = self.run_app(render_error=True)
        self.assertEqual(mock.bow.state, 'FAULT')
        self.assertIn('APPLICATION_ERROR', mock.bow.fault)


if __name__ == '__main__':
    unittest.main()
