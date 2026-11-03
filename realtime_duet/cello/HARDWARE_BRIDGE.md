# Hardware bridge: implemented, disabled until site inputs are supplied

`cello_adapter/phase_servo.py` is the actual execution engine; `hardware.py` binds it to RTDE and the original calibrated geometry. This supersedes the earlier unconditional hardware stub. No physical connection or command was executed during development.

## What changes within a bow

The engine uses continuous quarter-note phase, not a fixed note duration. Each four-beat bow follows a quintic progress curve between an explicitly selected pair of existing calibrated bow fractions. The next bow traverses the same interval in reverse. Shared endpoints have zero curve velocity and acceleration. A speed change updates the ongoing bow's phase progression; it does not wait for the next bow boundary. Software limits velocity and per-tick velocity change, never jumps to catch up, and faults when the reviewed phase-tracking tolerance cannot be met.

Only `u` changes. Pose is calculated by adapter `geometry.py`, which preserves the reviewed upstream pose/rotation/depth formulas; only calibration constants are parsed from upstream, with no module execution. Deterministic tests compare the pure implementation to the isolated original pure functions. It needs NumPy, not MIDI/audio libraries. No coordinates, depths, speed, acceleration, force, torque, gain or clearance are invented as runtime defaults. The upstream bow/depth/clearance/calibrated-speed/acceleration envelopes are checked before opening drivers. Existing `ServoStrokePlayer.play_stroke` was not used directly because its duration is fixed and its callback has no reviewed cancellation protocol; its absolute-pose servo and finally-stop pattern are reused in a new phase-driven worker.

The command-level speed/acceleration limits come from the explicit trajectory integrator. They must **not** be interpreted as controller-enforced limits: official ur_rtde documentation says servoL's speed and acceleration arguments are unused in the current version. The same documentation says RTDEControlInterface is not thread-safe, so one worker exclusively owns live control calls; Stop does not issue a concurrent driver call. [Official API](https://sdurobotics.gitlab.io/ur_rtde/pages/reference/api.html)

## Cancellation and actual stopping

Stop latches a cancellation event and prevents the next unsent target. The worker exits and invokes `servoStop()` in `finally`, including on rejected commands, safety faults and exceptions. A command already entering the driver cannot be recalled. No retake, lift, positioning, sensor tare, protective-stop unlock or reconnection is automatic.

Status distinguishes:

- `software_cancel_latched_physical_stop_pending`: cancellation recorded; driver may still be blocked.
- `driver_stop_returned_unverified`: stop API returned; physical standstill still unverified.
- `driver_stop_failed:...`: stop API raised/rejected.

The software ACK remains `stopped_latched` and never represents physical standstill. A driver call, OS stall, dead process or broken network can prevent the stop call from arriving. The onsite controller watchdog/protective-stop/E-stop strategy must be validated separately. The bridge does not claim hard real-time or a safety-rated stop. A blocked worker is not disconnected concurrently; pending physical status is logged.

Shutdown preserves a driver's recorded result across repeated Stop/close calls. An alive worker yields `driver_shutdown_incomplete:...`; disconnect errors are separately retained. Hardware process exit is **4** for failed/pending/incomplete/unknown shutdown, **3** when the musical run ended normally and the stop API returned but physical standstill remains unverified, or **1** for a run fault with the API returned. None of these is success code 0. Mock success remains exit 0. A False result during failed-connection cleanup is also reported as uncertain cleanup.

The same driver owner thread now executes the entire motion → stop → disconnect lifecycle. Closing before the first bow creates a cancellation-only owner, which sends no motion. `close()` performs one bounded join using `stop_wait_s`, including both stop and disconnect; it never calls either driver method synchronously. A blocked stage reports `driver_shutdown_incomplete:stopping` or `:disconnecting` with the preserved stop result. Repeated close waits on the same owner, without retrying driver calls. No concurrent disconnect, forced thread termination, automatic restart, or physical standstill claim is made. This is a software wait budget subject to OS/Python scheduling, not a physical stopping-time guarantee. Connection establishment and failed-connection cleanup occur before this backend lifecycle and are not covered by this close budget.

Before every streamed target the port checks driver connectivity, control program, reviewed NORMAL/REDUCED safety modes, force norm, torque norm, actual TCP position and orientation against reviewed tolerances. Force is the reported RTDE wrench, not a proven contact-force estimator. The operator must already have placed the bow at the selected starting pose using an approved procedure. Misplacement faults; this program does not move into position.

## Exact missing onsite inputs

These are required before actual execution; no production manifest or fake approval has been generated.

1. User acceptance of open-A3 rhythm substitution, selected session/domain, expiration time, and explicit air/contact commissioning mode.
2. Actual robot IP; existing reviewed calibration checkout and compatible Python 3.11/3.12 hardware environment. The narrowed hardware binding requires only `numpy` and `ur-rtde`. It no longer imports upstream recording/controller modules, MIDI, sounddevice, soundfile or PortAudio. See MACOS_QUICKSTART.md for the actual Mac target and existing-environment-first procedure. None was installed for this task.
3. All fields of `ServoLimits` below, chosen from existing calibration and onsite review. No values are inferred from the synthetic tests.
4. Real onsite records and their hashes. Both concrete callbacks are now supplied in `cello_adapter/site_gate.py`: `validate_session(review, session_id, clock_domain)` validates the scoped operator record before robot imports; `validate_connected_session(review, control, receive, geometry)` repeats the shared checks and queries installation/safety state read-only. A single manifest expiry governs the session. The onsite record contains five confirmations and one optional note, not twelve separately authored evidence paragraphs. Users need not write Python callbacks for this bundled path. The gate implementation itself still needs review. It is a new checklist gate, not an existing G1 approval. Operator statements about clearance, gravity, sensor zero and watchdog remain statements requiring actual onsite verification; software cannot establish those facts. Any additional site-mandated gate must remain in force and be integrated before execution; the bundled gate cannot silently replace it.
5. Independent review of the source/manifests, driver-stop behavior, controller watchdog and onsite E-stop. Supply the manifest's independently recorded SHA256 at invocation. A hash proves integrity, not safety or permission.

## Manifest schema (no default values)

| Field | Required content |
|---|---|
| `session_id`, `clock_domain`, `expires_unix_s` | Exact selected run identity and Unix expiration |
| `open_a_rhythm_approved`, `mode` | True only after user approval; `air` or `contact` |
| `robot_ip`, `repo_root`, `gate_path` | Actual controller address, original calibration checkout, applicable gate module |
| `review_status`, `onsite_checklist_path` | Review state and hash-pinned operator checklist. Shipped files are UNAPPROVED and rejected. |
| `allowed_safety_modes` | Explicit subset of upstream NORMAL/REDUCED codes 1 and 2 |
| `environment` | Exact reviewed `CELLO_*` environment overrides, excluding forced `CELLO_NO_AUTORECOVER=1` |
| `pins` | Automatically generated identities for 9 adapter runtime modules, 3 upstream data/profile source files, and the onsite record (13 paths). Optional actual operating-profile/data inputs are also pinned. Unrelated RL/analysis/test files are excluded. |
| `limits` | Explicit reviewed physical/timing limits; may come from one existing `operating_profile_path`. Bow length and unchanged upstream period/lookahead/gain are derived automatically when omitted. |
| `runtime_versions` | Automatically inventoried Python and required package versions from the selected real hardware environment; not hand-entered guesses |

`limits`: `u_low`, `u_high`, `bow_length_m`, `speed_max_m_s`, `acceleration_max_m_s2`, `tracking_error_max_m`, `period_s`, `max_tick_s`, `clock_timeout_s`, `lookahead_s`, `gain`, `depth_m`, `lift_m`, `force_max_n`, `torque_max_nm`, `pose_error_max_m`, `rotation_error_max_rad`, `stop_wait_s`, `sensor_timeout_s`. Robot telemetry timestamp must advance within sensor_timeout_s; review expiry is also checked during streaming.

`u_low/u_high` must lie inside upstream U_MIN/U_MAX; bow length must equal calibrated geometry; depth and lift cannot exceed existing envelopes; speed cannot exceed CALIBRATED_SPEED_MAX; acceleration cannot exceed ACCEL_MAX. Air mode requires positive reviewed lift; contact mode requires explicit zero lift. Active TCP/payload/CoG must match the original verified installation. Shared comparison permits only 1e-9 absolute SI-unit serialization/readback roundoff, with zero relative tolerance; this is not a physical installation offset allowance. Physical pose, force and motion envelopes are unchanged. The gate must verify gravity/tool mounting/sensor zero and any additional site restrictions. No installation writes occur.

## Commands

Start from `configs/hardware-review.UNAPPROVED.json` and `configs/onsite-checklist.UNAPPROVED.json`. They contain null physical limits and false checks; do not run them as approvals. The template gate_path points to the concrete bundled implementation. Read-only full missing-input report (currently BLOCKED):
```powershell
python -m cello_adapter.preflight_check --review configs/hardware-review.UNAPPROVED.json --report test-results/preflight-missing-inputs.json
```
This diagnostic and actual runtime preflight call the same read-only `validate_review` function, including operator scope, expiry, source identities, dependencies and calibration envelope. It never imports drivers or grants approval. The separate `tools/prepare_candidate_inventory.py` calculates candidate hashes, session/domain, versions and four derived calibration/profile values without marking any approval or onsite confirmation true. See SOURCE_AUDIT.md for the actual historical calibration/gate sources and why they cannot be treated as current approval.

Safe static preflight, **no driver imports/connections** (replace all placeholders with genuine reviewed inputs):
```powershell
python -m cello_adapter --backend hardware --session RUN --clock-domain DOMAIN --hardware-review /absolute/path/to/review/session.json --review-sha256 REVIEWED_SHA256
```

Actual onsite execution additionally requires `--execute-hardware --attended --arm`, a fresh log and a listener port. This command was **not run** in this task. Both preflight and execution recheck the review hash and source/data pins. Review expiration is checked before opening hardware. Explicit runtime enabling is not itself proof that onsite conditions are safe.

Mock receiver for immediate conductor integration remains unchanged:
```powershell
python -m cello_adapter --session rehearsal-001 --clock-domain local-rehearsal --listen 8766 --arm --log rehearsal-001.jsonl
```

## Software evidence

`python -m unittest discover -s tests -v` exercises the same PhaseServo.step used by the real worker with fake ports: full 12 bows, within-bow tempo changes, continuous bounded targets, cancellation before send, stale intent, loop stalls, infeasible jumps, force/torque/pose/safety rejection, failed servoL and failed servoStop. Synthetic review fixtures are temporary, never connect, and deliberately raise if their fake gate is imported. No synthetic limits/approvals are shipped as a runnable hardware configuration.

Actual geometry/driver import, servo timing, wrench validity, audio quality, force contact, and physical Stop remain untested on the robot. No claims about those are inferred from the fake driver results.

Current candidate: `configs/hardware-review.CANDIDATE-UNAPPROVED.json`; candidate operator record remains false in all five categories. The Windows development interpreter is Python 3.10.11 and lacks ur-rtde; the actual target is the user's Mac and its existing Python 3.11/3.12 hardware environment, still awaiting inspection. No installation was attempted.
