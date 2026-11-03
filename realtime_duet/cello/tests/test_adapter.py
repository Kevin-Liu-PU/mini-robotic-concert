import json
import tempfile
import unittest
from pathlib import Path
from cello_adapter.core import Journal, MockBackend, Scheduler, hardware_backend


def msg(t, phase, seq, bpm=120, **kw):
    return dict(version=1, session_id="test", clock_domain="shared-local-run", sequence=seq,
                source_monotonic_ns=max(0, int(t * 1e9)), kind="CLOCK",
                beat_position=phase, tempo_bpm=bpm, state="running",
                meter=[4, 4], quality="tracked", **kw)


class AdapterTests(unittest.TestCase):
    def setUp(self):
        self.backend = MockBackend()
        self.s = Scheduler("test", self.backend, armed=True)

    def run_score(self, varying=False, drop=False):
        phase = 0.0
        t = 0.0
        seq = 0
        self.s.ingest(msg(0, 0, seq), 0)
        self.s.tick(0)
        while not self.s.terminal and t < 100:
            bpm = 120 if not varying or phase < 8 else (180 if phase < 24 else 72)
            t += 0.01
            phase += 0.01 * bpm / 60
            seq += 1
            # A short missed transport burst is held by local clock. In normal
            # visual dropout producer keeps sending, so backend sees no gap.
            if not drop or not 2.1 <= t <= 2.4:
                self.s.ingest(msg(t, phase, seq, bpm), t)
            self.s.tick(t)
        return t

    def starts(self):
        return [e for e in self.backend.events if e["kind"] == "bow_start"]

    def test_complete_twelve_whole_notes(self):
        t = self.run_score()
        self.assertEqual(self.s.reason, "score_complete")
        self.assertEqual([e["index"] for e in self.starts()], list(range(12)))
        self.assertAlmostEqual(t, 24, delta=.02)
        self.assertEqual(len([e for e in self.backend.events if e["kind"] == "bow_end"]), 12)

    def test_accelerate_and_decelerate_current_bow(self):
        self.run_score(varying=True)
        starts = self.starts()
        self.assertEqual(len(starts), 12)
        self.assertAlmostEqual(starts[4]["monotonic_s"] - starts[3]["monotonic_s"], 4/3, delta=.04)
        self.assertAlmostEqual(starts[9]["monotonic_s"] - starts[8]["monotonic_s"], 10/3, delta=.04)

    def test_short_dropout_no_duplicate(self):
        self.run_score(drop=True)
        self.assertEqual(len(self.starts()), 12)
        self.assertEqual(self.s.reason, "score_complete")

    def test_duplicate_out_of_order_ignored(self):
        self.s.ingest(msg(0, 0, 1), 0)
        self.s.tick(0)
        self.assertFalse(self.s.ingest(msg(.1, .2, 1), .1))
        self.assertFalse(self.s.ingest(msg(.2, .4, 0), .2))
        self.s.tick(.2)
        self.assertEqual(len(self.starts()), 1)

    def test_explicit_stop_even_duplicate_latches(self):
        m = msg(0, 0, 1)
        self.s.ingest(m, 0)
        self.s.tick(0)
        m["kind"] = "STOP"
        self.s.ingest(m, .1)
        self.s.ingest(msg(.2, .4, 2), .2)
        self.s.tick(.2)
        self.assertEqual(self.s.reason, "explicit_stop")
        self.assertIsNone(self.backend.active)
        self.assertEqual(len(self.starts()), 1)

    def test_heartbeat_timeout_latches(self):
        self.s.ingest(msg(0, 0, 0), 0)
        for i in range(12):
            self.s.tick(i * .1)
        self.assertEqual(self.s.reason, "heartbeat_timeout")
        self.assertFalse(self.s.ingest(msg(1.2, 2.4, 1), 1.2))

    def test_timeout_cannot_be_hidden_by_new_snapshot(self):
        self.s.ingest(msg(0, 0, 0), 0)
        self.s.ingest(msg(1.1, 2.2, 1), 1.1)
        self.assertEqual(self.s.reason, "heartbeat_timeout")

    def test_process_restart_same_session_refused(self):
        with tempfile.TemporaryDirectory() as d:
            for expected in (False, True):
                b = MockBackend()
                s = Scheduler("test", b, Journal(d), armed=True)
                s.ingest(msg(0, 0, 0), 0)
                s.tick(0)
                self.assertEqual(s.terminal, expected)
            self.assertEqual(s.reason, "session_already_used_or_journal_failed")
            s = Scheduler("new", MockBackend(), Journal(d), armed=True)
            m = msg(0, 0, 0)
            m["session_id"] = "new"
            s.ingest(m, 0)
            s.tick(0)
            self.assertFalse(s.terminal)

    def test_phase_jump_stops_without_catchup(self):
        self.s.ingest(msg(0, 0, 0), 0)
        self.s.tick(0)
        self.s.ingest(msg(.1, 20, 1), .1)
        self.assertEqual(self.s.reason, "phase_discontinuity")
        self.assertEqual(len(self.starts()), 1)

    def test_small_backward_correction_never_reverses_bow(self):
        self.s.ingest(msg(0, 0, 0), 0)
        self.s.tick(0)
        self.s.tick(.1)
        before = self.backend.progress
        self.s.ingest(msg(.1, .1, 1), .1)
        self.s.tick(.1)
        self.assertGreaterEqual(self.backend.progress, before)

    def test_late_join_refused(self):
        self.s.ingest(msg(2, 4, 0), 2)
        self.assertEqual(self.s.reason, "late_join_refused")
        self.assertEqual(len(self.starts()), 0)

    def test_foreign_session_cannot_start_or_stop(self):
        m = msg(0, 0, 0)
        m.update(session_id="other", kind="STOP")
        self.s.ingest(m, 0)
        self.assertFalse(self.s.terminal)
        self.assertEqual(len(self.starts()), 0)

    def test_stall_stops(self):
        self.s.ingest(msg(0, 0, 0), 0)
        self.s.tick(0)
        self.s.tick(.3)
        self.assertEqual(self.s.reason, "local_loop_stall")

    def test_bad_data_fails_closed(self):
        for field, value in [("tempo_bpm", float("nan")), ("beat_position", "1"), ("sequence", True), ("state", "oops")]:
            s = Scheduler("test", MockBackend())
            m = msg(0, 0, 0)
            m[field] = value
            s.ingest(m, 0)
            self.assertTrue(s.reason.startswith("invalid_message"))

    def test_stale_and_future_clock(self):
        for t in (0, 2):
            s = Scheduler("test", MockBackend())
            self.assertFalse(s.ingest(msg(t, 0, 0), 1))
            self.assertIsNone(s.snapshot)

    def test_armed_countin_does_not_play(self):
        m = msg(0, 0, 0)
        m["state"] = "ready"
        self.s.ingest(m, 0)
        self.s.tick(0)
        self.assertEqual(len(self.starts()), 0)

    def test_hardware_fails_before_import(self):
        import sys
        before = set(sys.modules)
        with self.assertRaisesRegex(RuntimeError, "HARDWARE BLOCKED"):
            hardware_backend()
        self.assertEqual(before, set(sys.modules))

    def test_disconnect_stop_clears_active(self):
        self.s.ingest(msg(0, 0, 0), 0)
        self.s.tick(0)
        self.s.stop("disconnect", .1)
        self.assertIsNone(self.backend.active)
        self.assertTrue(self.s.terminal)

    def test_disarmed_by_default(self):
        b = MockBackend()
        s = Scheduler("test", b)
        s.ingest(msg(0, 0, 0), 0)
        s.tick(0)
        self.assertEqual(b.events, [])

    def test_no_bpm_does_not_start(self):
        m = msg(0, 0, 0)
        del m["tempo_bpm"]
        self.s.ingest(m, 0)
        self.s.tick(0)
        self.assertEqual(len(self.starts()), 0)

    def test_wrong_domain_dropped(self):
        m = msg(0, 0, 0)
        m["clock_domain"] = "foreign"
        self.assertFalse(self.s.ingest(m, 0))
        self.assertIsNone(self.s.snapshot)

    def test_predicted_visual_dropout_keeps_playing_at_60(self):
        for i in range(4802):
            t = i / 100
            m = msg(t, t, i, 60)
            m["quality"] = "predicted" if 10 <= t <= 20 else "tracked"
            self.s.ingest(m, t)
            self.s.tick(t)
        self.assertEqual(self.s.reason, "score_complete")
        self.assertEqual(len(self.starts()), 12)

    def test_priority_stop_with_backlog(self):
        from cello_adapter.transport import LatestInbox
        inbox = LatestInbox("test", "shared-local-run")
        for i in range(1000):
            inbox.put(msg(i / 1000, i / 500, i))
        stop = msg(0, 0, 0)
        stop["kind"] = "STOP"
        inbox.put(stop)
        inbox.put(msg(1, 2, 1001))
        kind, data = inbox.take()
        self.assertEqual(data["kind"], "STOP")
        self.s.ingest(data, 1)
        self.assertEqual(self.s.reason, "explicit_stop")
        self.assertEqual(len(self.starts()), 0)

    def test_stop_before_first_bow_retires_session(self):
        with tempfile.TemporaryDirectory() as d:
            s = Scheduler("test", MockBackend(), Journal(d), armed=True)
            s.stop("explicit_stop", 0)
            b = MockBackend()
            restarted = Scheduler("test", b, Journal(d), armed=True)
            restarted.ingest(msg(0, 0, 0), 0)
            restarted.tick(0)
            self.assertEqual(restarted.reason, "session_already_used_or_journal_failed")
            self.assertFalse(any(e["kind"] == "bow_start" for e in b.events))

    def test_backward_clock_does_not_refresh_heartbeat(self):
        self.s.ingest(msg(0, 0, 0), 0)
        self.s.ingest(msg(.2, .4, 1), .2)
        self.assertFalse(self.s.ingest(msg(.3, .2, 2), .3))
        self.assertFalse(self.s.ingest(msg(.1, .5, 3), .3))
        self.assertEqual(self.s.last_received, .2)


if __name__ == "__main__":
    unittest.main()
