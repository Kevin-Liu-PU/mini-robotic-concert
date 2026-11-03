"""All numbers here are synthetic unit-test geometry, NOT robot calibration."""
from dataclasses import replace
import unittest
from cello_adapter.phase_servo import PhaseServo, ServoBackend, ServoFault, ServoLimits


def fake_limits():
    return ServoLimits(u_low=.2, u_high=.4, bow_length_m=1.0,
                       speed_max_m_s=1.0, acceleration_max_m_s2=10.0,
                       tracking_error_max_m=.05, period_s=.01, max_tick_s=.03,
                       clock_timeout_s=.1, lookahead_s=.1, gain=100,
                       depth_m=0, lift_m=0, force_max_n=1, torque_max_nm=1, pose_error_max_m=.01,
                       rotation_error_max_rad=.01, stop_wait_s=.1, sensor_timeout_s=.1)


class FakePort:
    def __init__(self):
        self.commands = []
        self.stops = 0
        self.safe = True
        self.accept = True
        self.fail_stop = False
        self.now = 0.0
        self.fail_after = None

    def check_safety(self, u):
        if not self.safe:
            raise ServoFault("fake force/program/pose safety failure")

    def pose(self, u):
        return [u, 0, 0, 0, 0, 0]

    def servo(self, *args):
        self.commands.append(args)
        return self.accept

    def begin_period(self):
        return self.now

    def end_period(self, token):
        self.now += .01
        if self.fail_after is not None and len(self.commands) >= self.fail_after:
            self.safe = False

    def stop(self):
        self.stops += 1
        if self.fail_stop:
            raise RuntimeError("fake disconnected stop")


class PhaseServoTests(unittest.TestCase):
    def setUp(self):
        self.port = FakePort()
        self.engine = PhaseServo(self.port, fake_limits())

    def run_phases(self, varying=False):
        phase = 0
        t = 0
        velocities = []
        while phase < 48:
            bpm = 60 if not varying or phase < 2 else (120 if phase < 23 else 45)
            self.engine.publish(phase, bpm, t)
            self.engine.step(t)
            velocities.append(self.engine.velocity)
            phase += .01*bpm/60
            t += .01
        self.engine.publish(48, 60, t)
        self.engine.step(t)
        self.engine.finish()
        return velocities

    def test_complete_12_bows_same_bounded_path(self):
        self.run_phases()
        self.assertGreater(len(self.port.commands), 4700)
        self.assertEqual(self.port.stops, 1)
        self.assertTrue(all(.2 <= c[0][0] <= .4 for c in self.port.commands))
        self.assertTrue(self.engine.cancel.is_set())

    def test_midbow_tempo_changes_velocity_and_position_continuity(self):
        velocities = self.run_phases(varying=True)
        self.assertTrue(all(abs(v) <= 1 for v in velocities))
        self.assertTrue(all(abs(b-a) <= .100001 for a,b in zip(velocities, velocities[1:])))
        positions = [c[0][0] for c in self.port.commands]
        self.assertTrue(all(abs(b-a) <= .010001 for a,b in zip(positions, positions[1:])))
        self.assertEqual(self.port.stops, 1)

    def test_cancel_prevents_next_unsent_command(self):
        self.engine.publish(0, 60, 0)
        self.engine.step(0)
        self.engine.request_stop()
        count = len(self.port.commands)
        self.assertFalse(self.engine.step(.01))
        self.assertEqual(len(self.port.commands), count)
        self.engine.finish()
        self.engine.finish()
        self.assertEqual(self.port.stops, 1)
        self.assertEqual(self.engine.stop_status, "driver_stop_returned_unverified")

    def test_midcommand_cancel_checked_before_send(self):
        original = self.port.pose
        def cancel_in_pose(u):
            self.engine.request_stop()
            return original(u)
        self.port.pose = cancel_in_pose
        self.engine.publish(0, 60, 0)
        self.assertFalse(self.engine.step(0))
        self.assertEqual(self.port.commands, [])

    def test_timeout_rejects_before_motion(self):
        self.engine.publish(0, 60, 0)
        with self.assertRaisesRegex(ServoFault, "timeout"):
            self.engine.step(.2)
        self.assertEqual(self.port.commands, [])

    def test_infeasible_phase_jump_no_catchup(self):
        self.engine.publish(2, 60, 0)
        with self.assertRaisesRegex(ServoFault, "infeasible"):
            self.engine.step(0)
        self.assertEqual(self.port.commands, [])

    def test_worker_fault_calls_stop_once(self):
        self.port.fail_after = 2
        self.engine.clock = lambda: self.port.now
        self.engine.publish(0, 60, 0)
        self.engine.run()
        self.assertEqual(self.port.stops, 1)
        self.assertIn("safety failure", self.engine.fault)
        self.assertEqual(len(self.port.commands), 2)

    def test_driver_rejection_calls_stop(self):
        self.port.accept = False
        self.engine.clock = lambda: 0
        self.engine.publish(0, 60, 0)
        self.engine.run()
        self.assertIn("rejected", self.engine.fault)
        self.assertEqual(self.port.stops, 1)

    def test_stop_failure_not_reported_as_stopped(self):
        self.port.fail_stop = True
        self.engine.finish()
        self.assertTrue(self.engine.stop_status.startswith("driver_stop_failed"))
        self.assertIsNotNone(self.engine.fault)

    def test_no_implicit_defaults_for_physical_limits(self):
        with self.assertRaises(TypeError):
            ServoLimits()
        with self.assertRaises(ValueError):
            replace(fake_limits(), acceleration_max_m_s2=float("nan")).validate()
        with self.assertRaises(ValueError):
            replace(fake_limits(), force_max_n=0).validate()

    def test_worker_stall_does_not_send_catchup_target(self):
        self.engine.publish(0, 60, 0)
        self.engine.step(0)
        self.engine.publish(.04, 60, .04)
        with self.assertRaisesRegex(ServoFault, "stall"):
            self.engine.step(.04)
        self.assertEqual(len(self.port.commands), 1)

    def test_curve_has_shared_endpoint_and_zero_velocity(self):
        epsilon = 1e-5
        for phase in range(4, 48, 4):
            before = PhaseServo.fraction(phase-epsilon)
            after = PhaseServo.fraction(phase+epsilon)
            self.assertAlmostEqual(before, after, places=10)

    def test_blocked_driver_reports_pending_without_concurrent_stop(self):
        import threading
        entered, release = threading.Event(), threading.Event()
        def blocking_period(token):
            entered.set()
            release.wait(1)
        self.port.end_period = blocking_period
        self.engine.clock = lambda: 0
        self.engine.publish(0, 60, 0)
        self.engine.start_worker()
        self.assertTrue(entered.wait(1))
        self.engine.request_stop()
        self.assertEqual(self.engine.wait_stopped(), "software_cancel_latched_physical_stop_pending")
        self.assertEqual(self.port.stops, 0)
        release.set()
        self.assertEqual(self.engine.wait_stopped(), "driver_stop_returned_unverified")
        self.assertEqual(self.port.stops, 1)


if __name__ == "__main__":
    unittest.main()
