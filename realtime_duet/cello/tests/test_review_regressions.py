"""Independent-review reproductions, fake drivers only; no network or robot."""
import threading
import time
import unittest
import contextlib
import io
import json
from pathlib import Path
import tempfile
from unittest.mock import patch
from cello_adapter.__main__ import result_exit_code
from cello_adapter.core import MockBackend, Scheduler
from cello_adapter.hardware import cleanup_failed_connection
from cello_adapter.phase_servo import PhaseServo, ServoBackend
from test_adapter import msg
from test_phase_servo import FakePort, fake_limits


class ReviewedShutdownRegressionTests(unittest.TestCase):
    def test_never_started_blocking_stop_has_bounded_close_and_one_owner(self):
        port = FakePort()
        engine = PhaseServo(port, fake_limits())
        backend = ServoBackend(engine)
        entered, release, returned = threading.Event(), threading.Event(), threading.Event()
        calls, results = [], []
        def blocked_stop():
            calls.append(("stop", threading.get_ident()))
            port.stops += 1
            entered.set()
            release.wait(2)  # harness failsafe, not a physical timeout
        def disconnect():
            calls.append(("disconnect", threading.get_ident()))
        def close():
            started = time.perf_counter()
            results.append((backend.close(), time.perf_counter()-started))
            returned.set()
        port.stop, port.disconnect = blocked_stop, disconnect
        caller = threading.Thread(target=close, daemon=True)
        caller.start()
        try:
            self.assertTrue(entered.wait(1))
            self.assertTrue(returned.wait(.5), "control thread blocked inside driver stop")
            status, elapsed = results[0]
            self.assertLess(elapsed, .5)  # scheduling margin; configured wait is .1s
            self.assertIn("driver_shutdown_incomplete:stopping", status)
            self.assertIn("physical_stop_pending", status)
            self.assertEqual(result_exit_code("explicit_stop", "hardware", status), 4)
            self.assertEqual([name for name, _ in calls], ["stop"])
            self.assertEqual(port.commands, [])
        finally:
            release.set()
            caller.join(1)
            if engine.thread: engine.thread.join(1)
        self.assertEqual(backend.close(), "driver_stop_returned_unverified")
        self.assertEqual([name for name, _ in calls], ["stop", "disconnect"])
        self.assertEqual(len({owner for _, owner in calls}), 1)
        self.assertNotEqual(calls[0][1], caller.ident)
        self.assertEqual(port.stops, 1)

    def test_started_worker_blocking_disconnect_has_bounded_close_and_one_owner(self):
        port = FakePort()
        engine = PhaseServo(port, fake_limits(), clock=lambda: 0)
        backend = ServoBackend(engine)
        entered, release, returned = threading.Event(), threading.Event(), threading.Event()
        calls, results = [], []
        def stop():
            calls.append(("stop", threading.get_ident()))
            port.stops += 1
        def blocked_disconnect():
            calls.append(("disconnect", threading.get_ident()))
            entered.set()
            release.wait(2)
        def close():
            results.append(backend.close())
            returned.set()
        port.stop, port.disconnect = stop, blocked_disconnect
        port.end_period = lambda token: engine.request_stop()
        engine.publish(0, 60, 0)
        engine.start_worker()
        caller = threading.Thread(target=close, daemon=True)
        try:
            self.assertTrue(entered.wait(1))
            caller.start()
            self.assertTrue(returned.wait(.5), "control thread blocked inside driver disconnect")
            self.assertIn("driver_shutdown_incomplete:disconnecting", results[0])
            self.assertIn("driver_stop_returned_unverified", results[0])
            self.assertEqual(result_exit_code("explicit_stop", "hardware", results[0]), 4)
            self.assertEqual(len({owner for _, owner in calls}), 1)
            self.assertEqual(calls[0][1], engine.thread.ident)
            self.assertNotEqual(calls[0][1], caller.ident)
        finally:
            release.set()
            if caller.ident is not None: caller.join(1)
            engine.thread.join(1)
        self.assertEqual(backend.close(), "driver_stop_returned_unverified")
        self.assertEqual([name for name, _ in calls], ["stop", "disconnect"])
        self.assertEqual(port.stops, 1)

    def test_close_preserves_successful_driver_return_but_not_physical_success(self):
        port = FakePort()
        engine = PhaseServo(port, fake_limits())
        backend = ServoBackend(engine)
        engine.finish()
        before = engine.stop_status
        self.assertEqual(backend.close(), before)
        engine.request_stop()
        self.assertEqual(engine.stop_status, "driver_stop_returned_unverified")
        self.assertEqual(port.stops, 1)
        self.assertEqual(result_exit_code("explicit_stop", "hardware", before), 3)

    def test_close_preserves_failed_driver_stop_and_returns_failure(self):
        port = FakePort()
        port.fail_stop = True
        engine = PhaseServo(port, fake_limits())
        backend = ServoBackend(engine)
        engine.finish()
        before = engine.stop_status
        self.assertTrue(before.startswith("driver_stop_failed:"))
        self.assertEqual(backend.close(), before)
        self.assertEqual(backend.close(), before)
        self.assertEqual(port.stops, 1)
        self.assertEqual(result_exit_code("explicit_stop", "hardware", before), 4)

    def test_alive_worker_close_is_incomplete_and_never_exit_zero(self):
        port = FakePort()
        engine = PhaseServo(port, fake_limits(), clock=lambda: 0)
        backend = ServoBackend(engine)
        entered, release = threading.Event(), threading.Event()
        def blocked_period(token):
            entered.set()
            release.wait(2)
        port.end_period = blocked_period
        engine.publish(0, 60, 0)
        engine.start_worker()
        try:
            self.assertTrue(entered.wait(1))
            status = backend.close()
            self.assertTrue(status.startswith("driver_shutdown_incomplete:"))
            self.assertEqual(result_exit_code("score_complete", "hardware", status), 4)
            self.assertEqual(port.stops, 0)
            self.assertFalse(backend.disconnect_attempted)
        finally:
            release.set()
            engine.thread.join(1)
        self.assertEqual(backend.close(), "driver_stop_returned_unverified")
        self.assertEqual(port.stops, 1)

    def test_disconnect_failure_not_hidden_by_successful_stop(self):
        port = FakePort()
        attempts = []
        def fail_disconnect():
            attempts.append(1)
            raise RuntimeError("fake disconnect failure")
        port.disconnect = fail_disconnect
        backend = ServoBackend(PhaseServo(port, fake_limits()))
        status = backend.close()
        self.assertTrue(status.startswith("driver_disconnect_failed:"))
        self.assertEqual(result_exit_code("explicit_stop", "hardware", status), 4)
        self.assertEqual(backend.close(), status)
        self.assertEqual(len(attempts), 1)

    def test_failed_connection_cleanup_false_stop_is_uncertain(self):
        class Driver:
            def __init__(self): self.disconnected = False
            def servoStop(self): return False
            def disconnect(self): self.disconnected = True
        control, receive = Driver(), Driver()
        errors = cleanup_failed_connection(control, receive)
        self.assertEqual(errors, ["stop:servoStop returned False"])
        self.assertTrue(control.disconnected and receive.disconnected)

    def test_mock_success_remains_success_and_unknown_hardware_is_not(self):
        self.assertEqual(result_exit_code("explicit_stop"), 0)
        self.assertEqual(result_exit_code("explicit_stop", "hardware", None), 4)
        self.assertEqual(result_exit_code("hardware_fault", "hardware", "driver_stop_returned_unverified"), 1)

    def test_actual_cli_exit_uses_fake_hardware_shutdown_outcome(self):
        # All hardware creation/preflight replaced with local test objects.
        # This exercises main() through STOP and finally, with no socket/driver.
        from cello_adapter.__main__ import main
        stop = msg(0, 0, 0)
        stop.update(kind="STOP", state="stopped")
        for status, expected in (("driver_stop_failed:fake", 4),
                                 ("driver_shutdown_incomplete:pending", 4),
                                 ("driver_stop_returned_unverified", 3)):
            with self.subTest(status=status), tempfile.TemporaryDirectory() as d:
                class Backend(MockBackend):
                    def close(self): return status
                argv = ["--backend", "hardware", "--session", "test", "--clock-domain", "shared-local-run",
                        "--hardware-review", "TEST_FIXTURE_ONLY", "--review-sha256", "TEST_FIXTURE_ONLY",
                        "--execute-hardware", "--attended", "--arm", "--log", str(Path(d)/"log.jsonl"),
                        "--journal", str(Path(d)/"journal")]
                with patch("cello_adapter.hardware.preflight", return_value=({}, None)), \
                     patch("cello_adapter.__main__.hardware_backend", return_value=Backend()), \
                     patch("sys.stdin", io.StringIO(json.dumps(stop)+"\n")), \
                     contextlib.redirect_stdout(io.StringIO()):
                    code = main(argv)
                self.assertEqual(code, expected)
                events = [json.loads(x) for x in (Path(d)/"log.jsonl").read_text().splitlines()]
                self.assertEqual(events[-1]["status"], status)
                self.assertEqual(events[-1]["exit_code"], expected)


class ReviewedFirstRunningRegressionTests(unittest.TestCase):
    def setUp(self):
        self.backend = MockBackend()
        self.s = Scheduler("test", self.backend, armed=True)
        ready = msg(0, 0, 0)
        ready["state"] = "ready"
        self.assertTrue(self.s.ingest(ready, 0))

    def assert_no_bow(self):
        self.assertFalse(any(e["kind"] == "bow_start" for e in self.backend.events))

    def test_review_reproduction_ready_then_phase_point_three(self):
        self.assertFalse(self.s.ingest(msg(.2, .3, 1), .2))
        self.s.tick(.2)
        self.assertEqual(self.s.reason, "late_join_refused")
        self.assert_no_bow()

    def test_fresh_source_zero_but_delayed_receipt_cannot_start_late(self):
        self.assertFalse(self.s.ingest(msg(.1, 0, 1), .2))
        self.assertEqual(self.s.reason, "late_join_refused")
        self.assert_no_bow()

    def test_delayed_first_tick_cannot_bypass_admission_check(self):
        self.assertTrue(self.s.ingest(msg(.1, 0, 1), .1))
        self.s.tick(.16)
        self.assertEqual(self.s.reason, "late_join_refused")
        self.assert_no_bow()

    def test_ready_then_fresh_zero_starts_once(self):
        self.assertTrue(self.s.ingest(msg(.1, 0, 1), .1))
        self.s.tick(.1)
        self.s.tick(.11)
        self.assertEqual(len([e for e in self.backend.events if e["kind"] == "bow_start"]), 1)

    def test_wrong_domain_then_late_running_still_refused(self):
        other = msg(.1, 0, 1)
        other["clock_domain"] = "wrong"
        self.assertFalse(self.s.ingest(other, .1))
        self.assertFalse(self.s.ingest(msg(.2, .3, 2), .2))
        self.assert_no_bow()

    def test_out_of_order_first_running_does_not_set_anchor(self):
        self.assertFalse(self.s.ingest(msg(0, 0, 0), .01))
        self.assertFalse(self.s.running_seen)
        self.assertTrue(self.s.ingest(msg(.1, 0, 1), .1))
        self.s.tick(.1)
        self.assertEqual(len([e for e in self.backend.events if e["kind"] == "bow_start"]), 1)


if __name__ == "__main__": unittest.main()
