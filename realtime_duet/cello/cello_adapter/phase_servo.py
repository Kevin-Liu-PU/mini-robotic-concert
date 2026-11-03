"""Phase-driven absolute-pose servo bridge; imports no hardware modules.

All dimensional limits are mandatory reviewed inputs. A worker owns the driver;
the transport thread only publishes intent/cancellation. Fake-port tests exercise
the same step() implementation used by the worker.
"""
from dataclasses import dataclass
import math
import threading
import time


@dataclass(frozen=True)
class ServoLimits:
    u_low: float
    u_high: float
    bow_length_m: float
    speed_max_m_s: float
    acceleration_max_m_s2: float
    tracking_error_max_m: float
    period_s: float
    max_tick_s: float
    clock_timeout_s: float
    lookahead_s: float
    gain: float
    depth_m: float
    lift_m: float
    force_max_n: float
    torque_max_nm: float
    pose_error_max_m: float
    rotation_error_max_rad: float
    stop_wait_s: float
    sensor_timeout_s: float

    def validate(self):
        for name, value in vars(self).items():
            if type(value) not in (int, float) or not math.isfinite(value):
                raise ValueError("invalid reviewed parameter: " + name)
        positive = set(vars(self)) - {"u_low", "u_high", "depth_m", "lift_m"}
        if any(getattr(self, name) <= 0 for name in positive):
            raise ValueError("reviewed limits must be positive")
        if not 0 <= self.u_low < self.u_high <= 1 or self.lift_m < 0:
            raise ValueError("invalid bow interval/lift")
        if self.max_tick_s < self.period_s or self.clock_timeout_s < self.period_s:
            raise ValueError("invalid timing limits")
        # API admissibility bounds, not selected physical tuning defaults.
        if not .03 <= self.lookahead_s <= .2 or not 100 <= self.gain <= 2000:
            raise ValueError("servo lookahead/gain outside RTDE API bounds")
        return self


class ServoFault(RuntimeError):
    pass


class PhaseServo:
    """port.check_safety(last_u), pose(u), servo(...), stop() are reviewed I/O.

The port's safety check must validate actual pose, force, program and safety
state. It must never recover/reconnect or move to a starting pose implicitly.
"""
    def __init__(self, port, limits, clock=None):
        self.port, self.limits = port, limits.validate()
        self.clock = clock or (lambda: time.perf_counter_ns() / 1e9)
        self.lock = threading.Lock()
        self.cancel = threading.Event()
        self.latest = None
        self.fault = None
        self.stop_status = "not_requested"
        self.u = limits.u_low
        self.velocity = 0.0
        self.last_tick = None
        self.thread = None
        self.last_phase = 0.0
        self.stop_called = False
        self.disconnect_attempted = False
        self.disconnect_error = None
        self.shutdown_phase = "not_started"

    def publish(self, phase, bpm, now):
        if self.cancel.is_set():
            return
        if any(not math.isfinite(v) for v in (phase, bpm, now)) or bpm <= 0:
            raise ValueError("invalid servo intent")
        with self.lock:
            self.latest = (phase, bpm, now)

    @staticmethod
    def fraction(phase):
        # Quintic endpoints have zero first/second derivative. Adjacent bows
        # share exactly the same endpoint, with no discontinuous reversal.
        index = min(11, int(max(0.0, phase) // 4))
        x = min(1.0, max(0.0, (phase - index * 4) / 4))
        smooth = min(1.0, max(0.0, 10*x**3 - 15*x**4 + 6*x**5))
        return smooth if index % 2 == 0 else 1 - smooth

    def step(self, now):
        if self.cancel.is_set():
            return False
        with self.lock:
            snapshot = self.latest
        if snapshot is None:
            return True  # no target until explicitly started
        phase, bpm, received = snapshot
        c = self.limits
        if now - received > c.clock_timeout_s or now < received:
            raise ServoFault("servo intent timeout/clock reversal")
        dt = c.period_s if self.last_tick is None else now - self.last_tick
        if not 0 < dt <= c.max_tick_s:
            raise ServoFault("servo worker tick stall")
        self.last_tick = now
        phase = max(self.last_phase, phase + (now - received) * bpm / 60)
        self.last_phase = phase
        if phase >= 48:
            self.cancel.set()
            return False
        target = c.u_low + (c.u_high-c.u_low) * self.fraction(phase)
        error_m = (target-self.u) * c.bow_length_m
        if abs(error_m) > c.tracking_error_max_m:
            raise ServoFault("phase trajectory infeasible: tracking error")
        # Limit commanded velocity and its per-tick change. No target snaps on
        # tempo/phase updates. If lag grows beyond reviewed tolerance, stop.
        speed = min(c.speed_max_m_s, abs(error_m)/dt,
                    math.sqrt(2*c.acceleration_max_m_s2*abs(error_m)))
        desired = math.copysign(speed, error_m) if error_m else 0.0
        dv = c.acceleration_max_m_s2 * dt
        velocity = max(self.velocity-dv, min(self.velocity+dv, desired))
        u = self.u + velocity * dt / c.bow_length_m
        if not c.u_low <= u <= c.u_high:
            raise ServoFault("bow boundary reached before controlled deceleration")
        self.port.check_safety(self.u)
        pose = self.port.pose(u)
        if len(pose) != 6 or any(not math.isfinite(v) for v in pose):
            raise ServoFault("invalid calibrated pose")
        # A concurrent Stop cancels the next unsent command; a command already
        # entering the driver cannot be recalled by this software latch.
        if self.cancel.is_set():
            return False
        if self.port.servo(pose, c.speed_max_m_s, c.acceleration_max_m_s2,
                           dt, c.lookahead_s, c.gain) is False:
            raise ServoFault("servoL rejected target")
        self.u, self.velocity = u, velocity
        return True

    def finish(self):
        with self.lock:
            if self.stop_called:
                return
            self.stop_called = True
            self.stop_status = "software_cancel_latched_physical_stop_pending"
        try:
            self.port.stop()
            with self.lock:
                self.stop_status = "driver_stop_returned_unverified"
        except Exception as exc:
            with self.lock:
                self.stop_status = "driver_stop_failed:" + str(exc)
                self.fault = self.fault or self.stop_status

    def run(self):
        try:
            while not self.cancel.is_set():
                period = self.port.begin_period()
                if not self.step(self.clock()):
                    break
                self.port.end_period(period)
        except Exception as exc:
            self.fault = str(exc)
            self.cancel.set()
        finally:
            self.shutdown_phase = "stopping"
            self.finish()
            self.shutdown_phase = "disconnecting"
            # The same owner performs the entire driver lifecycle. A blocked
            # stop must finish before disconnect; callers only wait, never race
            # either operation with a second driver call or force thread exit.
            if not self.disconnect_attempted:
                self.disconnect_attempted = True
                disconnect = getattr(self.port, "disconnect", None)
                if disconnect:
                    try:
                        disconnect()
                    except Exception as exc:
                        self.disconnect_error = "driver_disconnect_failed:" + str(exc)
            self.shutdown_phase = "finished"

    def start_worker(self):
        with self.lock:
            if self.thread is not None or self.cancel.is_set():
                raise ServoFault("servo session cannot restart")
            self.thread = threading.Thread(target=self.run, name="reviewed-phase-servo", daemon=True)
            self.thread.start()

    def ensure_shutdown_worker(self):
        self.request_stop()
        with self.lock:
            if self.thread is None:
                # Cancel is already latched: run() cannot enter the motion loop.
                # Even a session never started gets an owner for stop/disconnect.
                self.thread = threading.Thread(target=self.run, name="reviewed-phase-servo", daemon=True)
                self.thread.start()

    def request_stop(self):
        with self.lock:
            # Repeated software cancellation must never erase a driver's known
            # terminal outcome (including failure) during close()/cleanup.
            if self.stop_status == "not_requested":
                self.stop_status = "software_cancel_latched_physical_stop_pending"
        self.cancel.set()

    def wait_stopped(self):
        if self.thread:
            self.thread.join(self.limits.stop_wait_s)
        return self.stop_status


class ServoBackend:
    """Scheduler-compatible nonblocking backend. Does not place/tare the bow."""
    def __init__(self, engine, sink=None):
        self.engine, self.sink = engine, sink
        self.started = False

    @property
    def disconnect_attempted(self):
        return self.engine.disconnect_attempted

    @property
    def disconnect_error(self):
        return self.engine.disconnect_error

    @property
    def fault(self):
        return self.engine.fault

    def start(self, index, phase, bpm, now):
        if not self.started:
            if index != 0:
                raise ServoFault("hardware late join refused")
            self.engine.port.check_safety(self.engine.limits.u_low)
            self.engine.publish(phase, bpm, now)
            self.engine.start_worker()
            self.started = True
        if self.sink:
            self.sink(dict(kind="bow_start_requested", index=index, phase_beats=phase,
                           bpm=bpm, monotonic_s=now, physical_execution_verified=False))

    def update(self, phase, bpm, now):
        if self.fault:
            raise ServoFault(self.fault)
        self.engine.publish(phase, bpm, now)

    def end(self, now):
        # Adjacent notes share a position-continuous global trajectory. Do not
        # reset u, retake, or restart the driver at each logical bow boundary.
        pass

    def stop(self, reason, now):
        self.engine.request_stop()
        if self.sink:
            self.sink(dict(kind="stop", reason=reason, monotonic_s=now,
                           physical_stop_status=self.engine.stop_status))

    def close(self):
        self.engine.ensure_shutdown_worker()
        status = self.engine.wait_stopped()
        # stop_wait_s bounds the one join across motion/stop/disconnect. It is a
        # software wait budget, never a guarantee of physical stopping time.
        if self.engine.thread.is_alive():
            return "driver_shutdown_incomplete:" + self.engine.shutdown_phase + "; stop_status=" + status
        if self.disconnect_error:
            return self.disconnect_error + "; stop_status=" + status
        return status
