"""App-loop, rendering and opt-in contracts with no devices or media writes."""
import contextlib
import io
import unittest
from unittest.mock import Mock, patch

import cv2
import numpy as np

from conductor import app
from conductor.multitrack import ConductorSession
from conductor.settings import load_config
from conductor.virtual_bow import VirtualBow, demo_hands, update_from_session
from conductor.virtual_bow_display import render_virtual_bow
from tests import test_mock_cello_app as harness


class VirtualBowAppTests(unittest.TestCase):
    def test_mode_is_opt_in_and_cannot_share_executor_or_clock_controls(self):
        self.assertFalse(app.parse_args([]).virtual_bow)
        for extra in (['--mock-cello'], ['--hold-clock'], ['--audio-clock'],
                      ['--num-hands', '1'], ['--list-cameras']):
            with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                app.parse_args(['--virtual-bow']+extra)
        for extra in (['--invert-bow-direction'], ['--virtual-bow-config', 'example.json']):
            with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                app.parse_args(extra)

    def test_actual_demo_loop_shows_two_hands_reverse_and_stop_on_horizontal_leg(self):
        states = []
        report = harness.AppLoopTests().run_app(enabled=False, virtual=True, seconds=5,
                                                config='config-single-hand.json', virtual_states=states)
        self.assertEqual(report['num_hands'], 2)
        self.assertIsNone(report['mock_cello'])
        self.assertIsNone(report['screen_clock'])
        self.assertFalse(report['audio_clock']['enabled'])
        self.assertIsNone(report['recording'])
        self.assertEqual({s['direction'] for s in states}, {-1, 0, 1})
        upward = next(s for s in states if 101.9 < s['time_s'] < 102)
        downward = next(s for s in states if 103.4 < s['time_s'] < 103.5)
        horizontal = next(s for s in states if 104.4 < s['time_s'] < 104.5)
        self.assertGreater(downward['speed'], upward['speed'])
        self.assertEqual(horizontal['speed'], 0)
        self.assertTrue(all(0 <= s['position'] <= 1 for s in states))
        self.assertFalse(report['virtual_bow']['enabled'])
        self.assertEqual(report['virtual_bow']['reason'], 'APPLICATION_EXIT')

    def test_space_pauses_only_virtual_motion_until_enter_reacquires(self):
        states = []
        harness.AppLoopTests().run_app(enabled=False, virtual=True, seconds=3,
            keys={61:32, 121:13}, virtual_states=states)
        paused = [s for s in states if 101.1 < s['time_s'] < 101.9]
        self.assertTrue(paused)
        self.assertTrue(all(not s['enabled'] and s['speed'] == 0 for s in paused))
        self.assertTrue(any(s['speed'] > 0 for s in states if s['time_s'] > 102.1))

    def test_canvas_shows_both_measured_speeds_trajectories_and_virtual_limits(self):
        session, bow = ConductorSession(), VirtualBow()
        for i in range(31):
            t = i/30
            session.update(demo_hands(t), t)
            update_from_session(bow, session, True, t)
        before = bow.snapshot()
        with patch.object(cv2, 'putText', wraps=cv2.putText) as drawn, \
             patch.object(cv2, 'line', wraps=cv2.line) as lines:
            canvas = render_virtual_bow(np.zeros((90,160,3),np.uint8), session, bow, 1,
                                         load_config(), dict(demo=True, recording=None))
        labels = [call.args[1] for call in drawn.call_args_list]
        self.assertEqual(canvas.shape, (800,1360,3))
        self.assertIn('A TIP', labels); self.assertIn('B TIP', labels)
        self.assertTrue(any('NO ROBOT' in text for text in labels))
        self.assertTrue(any(text.startswith('A: ') and 'vy' in text for text in labels))
        self.assertTrue(any(text.startswith('B: ') and 'vy' in text for text in labels))
        self.assertGreater(lines.call_count, 10)
        self.assertEqual(before, bow.snapshot())

    def test_app_view_recording_includes_virtual_state_with_source_timing(self):
        writer = Mock(); state = VirtualBow().snapshot(); canvas = object()
        app.record_rendered_frame(writer, canvas,
            dict(sequence=4, capture_monotonic_s=1.2), 1.25, virtual_state=state)
        writer.submit.assert_called_once_with(4,1.2,canvas)
        row = writer.add_diagnostic.call_args.args[0]
        self.assertEqual(row['virtual_bow'], state)
        self.assertEqual(row['rendered_monotonic_s'], 1.25)


if __name__ == '__main__':
    unittest.main()
