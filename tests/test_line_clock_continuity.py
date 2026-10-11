"""Line edits preserve cadence, never cross evidence boundaries or unlock stops.

All observations, keys, audio plans and UI labels are synthetic; no devices.
"""
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest

from conductor.app import adjust_line_for_key, audio_control_for_key
from conductor.audio_clock import AudioPlanner
from conductor.multitrack import ConductorSession
from conductor.numeric_trace import NumericTrace
from conductor.screen_clock import ScreenClock
from conductor.screen_display import audio_status_lines
from tests.test_beat_line import Hand, session_run
from tests.support.clocks import simulate, regular


def established(bpm=60):
    clock = simulate(regular(bpm, 12))[0]
    clock.drain_events()
    return clock


def advance(clock, end, beat=None):
    while clock.last_step + .02 < end - 1e-9:
        t = clock.last_step + .02
        clock.step(t, frame_time=t, track_good=True)
    return clock.step(end, frame_time=end, track_good=True, beat_time=beat)


class LineClockContinuityTests(unittest.TestCase):
    def test_60_and_120_keep_schedule_sound_and_counts_at_edit(self):
        for bpm in (60, 120):
            with self.subTest(bpm=bpm):
                c = established(bpm)
                s = ConductorSession()
                p = AudioPlanner(); p.toggle_mute(c.last_step)
                old = p.publish(c, c.last_step)
                before = (c.period, c.next_tick, c.last_tick, c.observed_count, c.predicted_count)
                self.assertTrue(adjust_line_for_key(s, ord('w'), c.last_step + .001, c))
                plan = p.publish(c, c.last_step + .001, c.drain_events())
                self.assertEqual(before, (c.period, c.next_tick, c.last_tick, c.observed_count, c.predicted_count))
                self.assertEqual((old.generation, old.ticks), (plan.generation, plan.ticks))
                self.assertTrue(plan.active); self.assertFalse(p.muted)
                self.assertEqual(c.snapshot(c.last_step)['phase_status'], 'UNCONFIRMED')
                self.assertEqual(c.snapshot(c.last_step)['current_evidence_confidence'], 0)
                start = c.last_step
                advance(c, start + 3)
                self.assertGreaterEqual(c.predicted_count-before[-1], int(3*bpm/60)-1)
                self.assertAlmostEqual(c.period, 60/bpm)

    def test_new_line_does_not_bridge_old_contact_or_update_on_first_contact(self):
        c = established(); now = c.last_step
        c.beat_line_changed(now + .001)
        for stamp in (now + .2, now + .95):
            advance(c, stamp, stamp)
            self.assertEqual(c.period, 1)
        advance(c, now + 1.7, now + 1.7)
        self.assertLess(c.period, 1)
        self.assertFalse(c.reentry)

    def test_duplicate_old_contact_cannot_seed_new_evidence(self):
        c = established(); now = c.last_step
        c.beat_line_changed(now + .01)
        advance(c, now + .02, now)
        self.assertIsNone(c.raw_anchor)
        self.assertEqual(len(c.return_intervals), 0)

    def test_repeated_line_edits_clear_partial_reentry(self):
        c = established(); now = c.last_step
        c.beat_line_changed(now + .001)
        advance(c, now + .1, now + .1)
        advance(c, now + .85, now + .85)
        self.assertEqual(len(c.return_intervals), 1)
        next_tick = c.next_tick
        c.beat_line_changed(now + .86)
        self.assertEqual(c.next_tick, next_tick)
        self.assertEqual(len(c.return_intervals), 0)
        self.assertIsNone(c.raw_anchor)

    def test_user_stop_stays_stopped_and_enter_still_resumes_saved_bpm(self):
        c = established(); now = c.last_step; c.stop(now)
        count = c.predicted_count
        c.beat_line_changed(now + .001)
        advance(c, now + 2, now + 2)
        self.assertEqual(c.stop_reason, 'USER_STOP')
        self.assertEqual(c.predicted_count, count)
        self.assertFalse(c.running)
        self.assertTrue(c.arm(now + 2.001))
        self.assertTrue(c.resume_pending); self.assertEqual(c.period, 1)

    def test_line_edit_never_unlocks_recovered_fault(self):
        c = established(); now = c.last_step; c.fail(now, 'CAMERA_ERROR')
        advance(c, now + .1)
        self.assertTrue(c.healthy)
        c.beat_line_changed(now + .101)
        advance(c, now + 2, now + 2)
        self.assertFalse(c.running); self.assertFalse(c.armed)
        self.assertEqual(c.snapshot(now + 2)['state'], 'FAULT_STOPPED')
        self.assertEqual(c.fault_reason, 'CAMERA_ERROR')

    def test_unestablished_clock_needs_all_new_intervals(self):
        c = ScreenClock(); c.step(0, frame_time=0, track_good=True)
        for t in (.1, 1.1, 2.1): advance(c, t, t)
        self.assertEqual(len(c.initial), 2)
        c.beat_line_changed(2.11)
        for t in (2.3, 3.3, 4.3):
            advance(c, t, t); self.assertFalse(c.running)
        advance(c, 5.3, 5.3)
        self.assertTrue(c.running); self.assertAlmostEqual(c.period, 1)

    def test_line_edit_during_warm_resume_keeps_three_interval_requirement(self):
        c = established(); now = c.last_step; c.stop(now); c.arm(now + .001)
        c.beat_line_changed(now + .002)
        for t in (now + .1, now + .85, now + 1.6):
            advance(c, t, t); self.assertTrue(c.resume_pending); self.assertEqual(c.period, 1)
        advance(c, now + 2.35, now + 2.35)
        self.assertFalse(c.resume_pending); self.assertLess(c.period, 1)

    def test_real_detector_has_no_synthetic_contact_after_line_moves(self):
        s = ConductorSession(); session_run(s, 0, 8)
        c = established(); now = c.last_step
        self.assertTrue(adjust_line_for_key(s, ord('s'), now, c))
        self.assertIsNone(s.engine.raw); self.assertFalse(s.engine.armed)
        self.assertIsNone(s.update([Hand(.25, .7, 'Right', 1)], now-.001))
        for i in range(1, 31):
            self.assertIsNone(s.update([Hand(.25, .7, 'Right', 1)], now+i/30))
        self.assertEqual(s.engine.count, 0)
        self.assertTrue(c.running)

    def test_clamped_line_does_not_clear_current_clock_evidence(self):
        s = ConductorSession(); s.set_beat_line(.1, 0)
        c = established(); before = (c.raw_anchor, c.phase_confirmations, c.next_tick)
        self.assertFalse(adjust_line_for_key(s, ord('w'), c.last_step, c))
        self.assertEqual(before, (c.raw_anchor, c.phase_confirmations, c.next_tick))
        self.assertFalse(c.drain_events())


class SoundControlEvidenceTests(unittest.TestCase):
    def test_early_m_is_recorded_as_refused_without_opening_audio(self):
        event = audio_control_for_key(None, ord('M'), 1)
        self.assertFalse(event['accepted']); self.assertTrue(event['muted'])
        self.assertEqual(event['reason'], 'SOURCE_NOT_READY')
        self.assertIsNone(audio_control_for_key(None, ord('w'), 1))

    def test_m_acceptance_and_failure_reasons_are_explicit(self):
        for accepted, muted, fault, reason in (
                (True, False, None, 'SOUND_ENABLED'), (True, True, None, 'MUTED'),
                (False, True, None, 'AUDIO_NOT_READY'), (False, True, 'ERROR', 'AUDIO_FAULT')):
            audio = SimpleNamespace(toggle_mute=lambda now: accepted,
                                    snapshot=lambda: dict(muted=muted, fault=fault))
            event = audio_control_for_key(audio, ord('m'), 1)
            self.assertEqual((event['accepted'], event['reason']), (accepted, reason))

    def test_control_and_fault_evidence_is_readable_before_close(self):
        for kind in ('AUDIO_CONTROL', 'CLOCK_CONTROL', 'SYSTEM_FAULT', 'LOOP_GAP_CONTEXT'):
            with tempfile.TemporaryDirectory() as directory:
                path = Path(directory)/'trace.jsonl'; trace = NumericTrace(path)
                try:
                    trace.write(dict(kind=kind, time_s=1))
                    self.assertEqual(json.loads(path.read_text())['kind'], kind)
                finally: trace.close()

    def test_ui_explains_muted_and_enabled_but_stopped_independently(self):
        c = established(); now = c.last_step
        audio = dict(state='MUTED', muted=True, playback_requests=0)
        self.assertIn('M: enable', audio_status_lines(c.snapshot(now), audio, now)[0])
        c.stop(now)
        self.assertIn('CLOCK STOPPED', audio_status_lines(c.snapshot(now), audio, now)[1])
        audio.update(state='ON', muted=False)
        self.assertEqual(audio_status_lines(c.snapshot(now), audio, now)[0], 'SOUND ON / CLOCK STOPPED')
        audio['last_control'] = dict(accepted=False, reason='SOURCE_NOT_READY', time_s=now)
        self.assertIn('M received: not ready', audio_status_lines(c.snapshot(now), audio, now)[1])


if __name__ == '__main__': unittest.main()
