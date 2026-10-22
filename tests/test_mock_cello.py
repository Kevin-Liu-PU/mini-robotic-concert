"""Standard-library-only proof; no camera, audio, GUI, network or hardware imports."""
import contextlib
import io
import math
import unittest
from unittest.mock import patch

from conductor.core import Beat
from conductor.app import parse_args, update_mock_cello
from conductor.mock_cello import MockCello, MockCelloControls
from conductor.mock_cello_demo import run_demo


class MockBowTests(unittest.TestCase):
    def test_startup_requires_explicit_arm(self):
        bow = MockCello()
        self.assertFalse(bow.offer(Beat(1, 1, None), 1))
        self.assertEqual(bow.state, 'DISARMED')
        self.assertTrue(bow.arm(2))
        self.assertTrue(bow.offer(Beat(2.1, 2, None), 2.1))
        self.assertEqual(bow.state, 'BUSY')
        bow.tick(2.6)
        self.assertEqual((bow.state, bow.started, bow.completed), ('DISARMED', 1, 1))
        self.assertFalse(bow.offer(Beat(2.7, 3, None), 2.7))

    def test_busy_drops_without_queue_even_at_completion_boundary(self):
        bow = MockCello()
        bow.arm(1)
        bow.offer(Beat(1.1, 1, None), 1.1)
        self.assertFalse(bow.arm(1.2))
        for t in (1.2, 1.3, 1.6):
            self.assertFalse(bow.offer(Beat(t, 2, None), t))
        bow.tick(1.6)
        bow.tick(100)
        self.assertEqual((bow.started, bow.completed, bow.snapshot()['queued']), (1, 1, 0))

    def test_pre_arm_same_time_stale_future_and_invalid_events(self):
        for event_time, delivery, reason in ((.9, 1.1, 'PRE_ARM'), (1, 1.1, 'PRE_ARM'),
                (1.1, 1.5, 'STALE'), (2, 1.1, 'INVALID_EVENT_TIME'),
                (math.nan, 1.1, 'INVALID_EVENT_TIME'), (math.inf, 1.1, 'INVALID_EVENT_TIME')):
            with self.subTest(reason=reason, event_time=event_time):
                bow = MockCello()
                bow.arm(1)
                self.assertFalse(bow.offer(Beat(event_time, 1, None), delivery))
                self.assertEqual(bow.reason, 'DROPPED_' + reason)
                self.assertEqual(bow.started, 0)

    def test_duplicate_and_reordered_watermark_survives_reset_and_rearm(self):
        bow = MockCello()
        bow.arm(1)
        bow.offer(Beat(1.1, 1, None), 1.1)
        bow.offer(Beat(1.2, 2, None), 1.2)  # Dropped while BUSY, still seen.
        bow.reset(1.21)
        bow.arm(1.22)
        for t in (1.2, 1.1):
            self.assertFalse(bow.offer(Beat(t, 2, None), 1.23))
            self.assertEqual(bow.reason, 'DROPPED_DUPLICATE_OR_REORDERED')
        # Detector count restarts at reset; timestamp still identifies an event.
        self.assertTrue(bow.offer(Beat(1.24, 1, None), 1.24))

    def test_stop_cancels_deadline_and_rearm_uses_new_event(self):
        bow = MockCello()
        bow.arm(1)
        bow.offer(Beat(1.1, 1, None), 1.1)
        bow.stop()
        bow.tick(1.7)
        self.assertEqual(bow.completed, 0)
        bow.arm(1.8)
        self.assertFalse(bow.offer(Beat(1.75, 2, None), 1.81))
        self.assertTrue(bow.offer(Beat(1.82, 3, None), 1.82))
        bow.tick(1.83)  # Old completion deadline cannot finish the new bow.
        self.assertEqual(bow.state, 'BUSY')

    def test_start_exception_latches_fault_until_explicit_reset(self):
        bow = MockCello()
        bow.arm(1)
        with patch.object(bow, '_start', side_effect=RuntimeError('injected mock failure')):
            self.assertFalse(bow.offer(Beat(1.1, 1, None), 1.1))
        bow.stop()
        bow.tick(2)
        self.assertFalse(bow.arm(2))
        self.assertEqual(bow.state, 'FAULT')
        self.assertIn('injected mock failure', bow.fault)
        bow.reset(2.1)
        self.assertEqual(bow.state, 'DISARMED')
        self.assertFalse(bow.offer(Beat(2.2, 2, None), 2.2))

    def test_completion_exception_latches_fault(self):
        bow = MockCello()
        bow.arm(1)
        bow.offer(Beat(1.1, 1, None), 1.1)
        bow._due = None  # Corrupt the mock's completion seam.
        bow.tick(1.2)
        self.assertEqual(bow.state, 'FAULT')
        self.assertFalse(bow.arm(2))

    def test_time_regression_and_nonfinite_fail_closed(self):
        for t in (0, math.nan, math.inf):
            bow = MockCello()
            bow.arm(1)
            bow.tick(t)
            self.assertEqual(bow.state, 'FAULT')
            self.assertFalse(bow.arm(2))

    def test_invalid_config(self):
        for v in (0, -1, math.nan, math.inf):
            with self.assertRaises(ValueError):
                MockCello(duration_s=v)
            with self.assertRaises(ValueError):
                MockCello(max_event_age_s=v)


class AppSeamTests(unittest.TestCase):
    def ready(self):
        mock = MockCelloControls()
        mock.step(1, sample=(1, .2, .3), event=None, source_token=('A', 1))
        return mock

    def arm(self, mock, now=1):
        mock.key(ord('a'), now)
        self.assertTrue(mock.key(ord('b'), now))

    def test_opt_in_and_interactive_requirement(self):
        self.assertFalse(parse_args([]).mock_cello)
        self.assertTrue(parse_args(['--demo', '--mock-cello']).mock_cello)
        for args in (['--headless', '--seconds', '1'], ['--list-cameras']):
            with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                parse_args(['--mock-cello'] + args)

    def test_enter_never_arms_and_also_clears_pending_confirmation(self):
        mock = self.ready()
        mock.key(ord('a'), 1)
        mock.key(13, 1)
        self.assertFalse(mock.key(ord('b'), 1))
        self.assertEqual(mock.bow.state, 'DISARMED')

    def test_repeated_confirmation_key_cannot_rearm(self):
        mock = self.ready()
        self.arm(mock)
        mock.step(1.1, sample=(1.1, .2, .3), event=Beat(1.1, 1, None), source_token=('A', 1))
        for i in range(12, 21):
            t = i / 10
            mock.step(t, sample=(t, .2, .3), event=Beat(t, i, None), source_token=('A', 1))
            self.assertFalse(mock.key(ord('b'), t))
        self.assertEqual((mock.bow.state, mock.bow.started, mock.bow.completed), ('DISARMED', 1, 1))

    def test_context_controls_clear_authorization(self):
        for key in ('x', ' ', 'r', 'w', 's', '1', '2', '\r', 'q', '\x1b'):
            mock = self.ready()
            self.arm(mock)
            mock.key(ord(key), 1.01)
            mock.step(1.1, sample=(1.1, .2, .3), event=Beat(1.1, 1, None), source_token=('A', 1))
            self.assertEqual(mock.bow.started, 0, key)

    def test_hand_loss_invalid_tip_and_track_change_clear_authorization(self):
        for sample, token in (((1.1, None, None), ('A', 1)),
                              ((1.1, .2, .3), ('A', 2)), ((1.1, .2, .3), None),
                              ((1.1, math.nan, .3), ('A', 1))):
            mock = self.ready()
            self.arm(mock)
            mock.step(1.1, sample=sample, event=None, source_token=token)
            mock.step(1.2, sample=(1.2, .2, .3), event=Beat(1.2, 1, None), source_token=('A', 1))
            self.assertEqual(mock.bow.started, 0)

    def test_gap_then_fresh_frame_does_not_auto_resume(self):
        mock = self.ready()
        self.arm(mock)
        mock.step(1.5, sample=(1.5, .2, .3), event=Beat(1.5, 1, None), source_token=('A', 1))
        self.assertEqual(mock.bow.started, 0)

    def test_stale_source_clears_pending_confirmation(self):
        mock = self.ready()
        mock.key(ord('a'), 1)
        mock.step(1.5, sample=None, event=None, source_token=('A', 1))
        mock.step(1.6, sample=(1.6, .2, .3), event=None, source_token=('A', 1))
        self.assertFalse(mock.key(ord('b'), 1.6))

    def test_replayed_frame_and_pre_arm_beat_are_rejected(self):
        mock = self.ready()
        self.arm(mock)
        mock.step(1.1, sample=(1, .2, .3), event=Beat(1, 1, None), source_token=('A', 1))
        self.assertEqual(mock.bow.started, 0)

    def test_prediction_free_ticks_cannot_start_bow(self):
        mock = self.ready()
        self.arm(mock)
        for i in range(11, 30):
            mock.step(i/10, sample=(i/10, .2, .3), event=None, source_token=('A', 1))
        self.assertEqual((mock.bow.state, mock.bow.started), ('ARMED', 0))

    def test_disabled_seam_does_not_inspect_session(self):
        update_mock_cello(None, None, None, None, 1)

    def test_synthetic_detector_through_actual_app_seam(self):
        result = run_demo()
        self.assertEqual([t['state'] for t in result['transitions']],
                         ['DISARMED', 'ARMED', 'BUSY', 'DISARMED'])
        self.assertEqual((result['final']['started'], result['final']['completed']), (1, 1))
        self.assertGreater(result['final']['dropped'], 0)


if __name__ == '__main__':
    unittest.main()
