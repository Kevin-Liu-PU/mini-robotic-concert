"""Gated lazy RTDE binding with pure geometry. No I/O at import time."""
import hashlib
import math
from pathlib import Path
import time
from .phase_servo import PhaseServo, ServoBackend, ServoFault
from .review_validation import installation_equal


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def preflight(review_path, expected_sha256, session, domain):
    """Exactly the same static validator used by the diagnostic report."""
    from .review_validation import validate_review
    review, limits, report = validate_review(review_path, expected_sha256, session, domain)
    if report["issues"]:
        raise ValueError("; ".join(item["field"]+": "+item["reason"] for item in report["issues"]))
    return review, limits


def cleanup_failed_connection(control, receive):
    """Best-effort independent cleanup; False stop result is not success."""
    errors = []
    if control is not None:
        try:
            if control.servoStop() is False:
                errors.append("stop:servoStop returned False")
        except Exception as exc:
            errors.append("stop:" + str(exc))
        try:
            control.disconnect()
        except Exception as exc:
            errors.append("control disconnect:" + str(exc))
    if receive is not None:
        try:
            receive.disconnect()
        except Exception as exc:
            errors.append("receive disconnect:" + str(exc))
    return errors


class UpstreamPort:
    """Uses original geometry/installation and RTDE servoL/servoStop APIs.

No reset(), prepare(), retake(), tare, dashboard unlock, or reconnect path.
The onsite operator must position the robot before enabling this session.
"""
    def __init__(self, control, receive, geometry, limits, safety_modes, expires_unix_s=None):
        self.c, self.r, self.g, self.limits = control, receive, geometry, limits
        self.safety_modes = safety_modes
        self.expires_unix_s = expires_unix_s
        self.last_sensor_stamp = None
        self.last_sensor_change = None

    def pose(self, u):
        g, c = self.g, self.limits
        if not g.U_MIN <= u <= g.U_MAX:
            raise ServoFault("target outside upstream bow envelope")
        pose = g.apply_depth(g.pose_at(u), g.CFG, c.depth_m)
        pose[:3] = pose[:3] + c.lift_m * g.press_direction(g.CFG)
        return [float(v) for v in pose]

    def check_safety(self, last_u):
        if self.expires_unix_s is not None and time.time() >= self.expires_unix_s:
            raise ServoFault("reviewed session expired")
        if not self.c.isConnected() or not self.r.isConnected() or not self.c.isProgramRunning():
            raise ServoFault("driver disconnected/control program stopped")
        stamp = self.r.getTimestamp()
        now = time.perf_counter_ns() / 1e9
        if not math.isfinite(stamp) or (self.last_sensor_stamp is not None and stamp < self.last_sensor_stamp):
            raise ServoFault("invalid/reversed robot telemetry timestamp")
        if stamp != self.last_sensor_stamp:
            self.last_sensor_stamp, self.last_sensor_change = stamp, now
        elif now-self.last_sensor_change > self.limits.sensor_timeout_s:
            raise ServoFault("stale robot telemetry")
        if int(self.r.getSafetyMode()) not in self.safety_modes:
            raise ServoFault("robot safety state outside reviewed modes")
        force = list(self.r.getActualTCPForce())
        if len(force) != 6 or not all(math.isfinite(v) for v in force):
            raise ServoFault("invalid force measurement")
        if math.sqrt(sum(v*v for v in force[:3])) > self.limits.force_max_n:
            raise ServoFault("reviewed force ceiling exceeded")
        if math.sqrt(sum(v*v for v in force[3:])) > self.limits.torque_max_nm:
            raise ServoFault("reviewed torque ceiling exceeded")
        actual = list(self.r.getActualTCPPose())
        expected = self.pose(last_u)
        if len(actual) != 6 or not all(math.isfinite(v) for v in actual):
            raise ServoFault("invalid TCP measurement")
        if math.dist(actual[:3], expected[:3]) > self.limits.pose_error_max_m:
            raise ServoFault("actual TCP outside reviewed target tolerance; no automatic positioning")
        # Reuse upstream rotation-vector conversion; do not compare axis-angle
        # components directly across the double-cover hemisphere boundary.
        ra, rb = self.g._rv_to_matrix(actual[3:]), self.g._rv_to_matrix(expected[3:])
        cosine = (float((ra.T @ rb).trace()) - 1) / 2
        angle = math.acos(max(-1.0, min(1.0, cosine)))
        if angle > self.limits.rotation_error_max_rad:
            raise ServoFault("actual orientation outside reviewed tolerance")

    def servo(self, *args):
        return self.c.servoL(*args)

    def begin_period(self):
        return self.c.initPeriod()

    def end_period(self, token):
        self.c.waitPeriod(token)

    def stop(self):
        # Same finally-path API as upstream ServoStrokePlayer. Return does not
        # establish physical standstill; independent onsite checks remain.
        if self.c.servoStop() is False:
            raise ServoFault("servoStop reported failure")

    def disconnect(self):
        self.c.disconnect()
        self.r.disconnect()


def open_hardware(review_path, expected_sha256, session, domain, *, execute=False,
                  attended=False, armed=False, sink=None):
    if not (execute and attended and armed):
        raise ValueError("hardware requires explicit execute + attended + arm")
    review, limits = preflight(review_path, expected_sha256, session, domain)
    from . import site_gate as gate
    validator = getattr(gate, "validate_session", None)
    if not callable(validator) or validator(review, session, domain) is not True:
        raise ValueError("existing onsite gate refused session")
    # Recheck source pins after gate and immediately before hardware imports.
    preflight(review_path, expected_sha256, session, domain)
    # Upstream source is data only: no recording/MIDI module is imported.
    # Pure geometry is the reviewed adapter implementation, not executed AST.
    from .review_validation import calibration_data
    from .geometry import CalibratedGeometry
    geom = CalibratedGeometry(calibration_data(review))
    geom._verify_geometry()
    if not geom.U_MIN <= limits.u_low < limits.u_high <= geom.U_MAX:
        raise ValueError("reviewed bow interval exceeds existing envelope")
    if not -geom.MAX_OUTWARD_DEPTH <= limits.depth_m <= geom.MAX_INWARD_DEPTH:
        raise ValueError("reviewed depth exceeds existing envelope")
    if limits.lift_m > geom.LIFT_M or not math.isclose(limits.bow_length_m, geom.BOW_LENGTH, rel_tol=1e-9):
        raise ValueError("reviewed clearance/bow length differs from existing calibration")
    if limits.acceleration_max_m_s2 > geom.ACCEL_MAX:
        raise ValueError("reviewed acceleration exceeds upstream ceiling")
    if limits.speed_max_m_s > geom.CALIBRATED_SPEED_MAX:
        raise ValueError("reviewed speed exceeds upstream calibrated range")
    import rtde_control
    import rtde_receive
    control = receive = None
    try:
        # This is the sole connection site; never reached by mock/preflight.
        control = rtde_control.RTDEControlInterface(review["robot_ip"])
        receive = rtde_receive.RTDEReceiveInterface(review["robot_ip"])
        # The connection may upload the RTDE control script. It does not call
        # upstream constructors that alter TCP/payload or position the robot.
        # Require the already-established installation to match calibration.
        tcp = list(control.getTCPOffset())
        if not installation_equal(tcp, geom._rec.VERIFIED_TCP):
            raise ServoFault("active TCP differs from calibrated installation")
        if not installation_equal([receive.getPayload()], [geom._rec.VERIFIED_PAYLOAD_KG]):
            raise ServoFault("active payload differs from calibrated installation")
        if not installation_equal(receive.getPayloadCog(), geom._rec.VERIFIED_PAYLOAD_COG):
            raise ServoFault("active payload CoG differs from calibrated installation")
        # Site gate owns additional installation checks (payload CoG/gravity,
        # tool mounting and reviewed sensor zero); no automatic setup/tare.
        postcheck = getattr(gate, "validate_connected_session", None)
        if not callable(postcheck) or postcheck(review, control, receive, geom) is not True:
            raise ServoFault("onsite installation/safety validation failed")
        port = UpstreamPort(control, receive, geom, limits, review["allowed_safety_modes"], review["expires_unix_s"])
        port.check_safety(limits.u_low)
        return ServoBackend(PhaseServo(port, limits), sink)
    except Exception as original:
        cleanup_errors = cleanup_failed_connection(control, receive)
        if cleanup_errors:
            raise ServoFault(str(original) + "; cleanup outcome uncertain: " + "; ".join(cleanup_errors)) from original
        raise
