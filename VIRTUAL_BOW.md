# Two-hand virtual bow prototype

This is a separate, opt-in screen simulation for a practical demonstration.
There is no robot adapter, connection, motion command, audio output or new
dependency. The original beat/BPM, responsive/hold and mock-one-shot modes
remain available unchanged when `--virtual-bow` is omitted.

## Run

From the repository root, with the existing project `.venv`:

```sh
# Synthetic mirrored example: opens only the local virtual display, no camera/model.
.venv/bin/python -B -m conductor.app --virtual-bow --demo --seconds 30

# Synthetic console check: no camera, window, recording or audio.
.venv/bin/python -B -m conductor.app --virtual-bow --virtual-bow-config config-virtual-bow.json --demo --headless --seconds 5 --report logs/virtual-bow-demo.json

# Later user-started live trial: verified built-in Mac camera, TWO hands.
.venv/bin/python -B -m conductor.app --virtual-bow --backend avfoundation --camera-built-in --seconds 180 --report logs/virtual-bow-live.json
```

Use the direct app command for this separate mode. The existing
`launch-responsive-mac.command` intentionally enables the original hold clock
and is not a virtual-bow launcher. Virtual bow rejects `--hold-clock`,
`--audio-clock`, `--mock-cello` and explicit `--num-hands 1` together with it.
It requests two hands from the existing MediaPipe pipeline, including when a
one-hand camera config is supplied. Camera name/index selection is still
available instead of `--camera-built-in`.

No live camera or recording was started for implementation or testing.

## Mapping and assumptions

The adapter reads **both existing A/B track segments and their observed index
fingertips (landmark 8)**. It does not select only MAIN, consume beat events or
predicted clock ticks, or modify the tracker/detector. A/B are geometric track
segments, not anatomical identities. Existing fingertip trajectory rendering
is reused; velocity is calculated from raw observed tips, not smoothed trails.

For each synchronized pair of observations:

1. Calculate each hand's `vx = dx/dt`, `vy = dy/dt` independently. Image y grows
   downward; coordinates range from 0 to 1 across image width and height.
2. Both `vy` values must exceed the vertical deadband in magnitude and have the
   same sign. Upward movement maps to **RIGHT**; downward maps to **LEFT**.
3. Calculate each hand's magnitude `sqrt(vx*vx + vy*vy)`, then take their mean.
   Never average signed horizontal velocities: mirrored horizontal movement
   contributes speed instead of cancelling to zero.
4. Reject excessive magnitude disagreement (larger/smaller speed > configured
   ratio), smooth the agreed scalar magnitude, multiply by gain, and cap speed.
   On a direction reversal the new sign and magnitude take effect immediately;
   the smoothing filter cannot retain the prior sign.
5. Integrate the bounded speed into a virtual position from 0 to 1. Its initial
   position is 0.5. At a limit, outward motion stops; a fresh agreeing inward
   command can move away. There is no wraparound or automatic reversal.

Speeds are **normalized image units per second**, and bow speed is **fractions of
virtual travel per second**. These are not calibrated physical distances, motor
velocities or force commands. Camera framing/aspect ratio and hand distance
affect the measurements. The exact triangle geometry and horizontal-leg meaning
have not been demonstrated. There is no triangle recognizer: horizontal-only
motion, one stationary hand, or opposing vertical directions stop the bow.
The synthetic example merely illustrates slow upward diagonal motion, faster
downward diagonal motion and a horizontal return, mirrored between hands.

## Configuration and stops

Pass `--virtual-bow-config config-virtual-bow.json` to load the editable mapping.
Without it the same defaults are used. Add `--invert-bow-direction` to map up to
left and down to right; this changes direction only.

| Setting | Default | Meaning |
| --- | --- | --- |
| `speed_gain` | 0.6 | Convert mean hand magnitude to virtual travel speed |
| `max_speed` | 0.5 | Maximum travel/s; configuration limited to <=1 |
| `vertical_deadband` | 0.025 | Minimum absolute vertical speed for each hand |
| `smoothing_tau_s` | 0.06 | Scalar speed smoothing time constant |
| `max_age_s` | 0.20 | Maximum age of an observation |
| `max_gap_s` | 0.20 | Maximum observation/UI gap before stopping |
| `max_hand_speed` | 4.0 | Reject implausibly fast normalized motion |
| `max_speed_ratio` | 3.0 | Largest accepted ratio of hand magnitudes |
| `invert_direction` | false | Swap virtual left/right mapping |

Missing hands, invalid tips, stale/future/reordered/duplicate timestamps,
unsynchronized observations and tracking ambiguity stop motion. A new track
generation or recovery after loss requires a fresh velocity baseline followed
by another valid observation. UI stalls never produce catch-up movement.
Observation age and gap settings cannot exceed 0.25 seconds. These are virtual
prototype guards, not physical robot safety controls.

The virtual input starts enabled but stationary until two valid frames arrive.
It resumes automatically from a tracking interruption only after fresh evidence;
this continuous virtual behavior is separate from the older one-shot mock.
**Space/X** latches a manual pause. **Enter** enables fresh acquisition again.
**R** centers the virtual bow and pauses it. **Q/Esc** exits. Invalid clock time
also disables input. No gesture or incoming observation releases a manual pause.

## Display and later recording

The display shows both colored fingertip trajectories, each measured speed and
vertical velocity, the current stop/move reason, commanded direction/speed, and
a moving virtual carriage with travel limits. It does not present BPM as the
controller for this mode.

The prepared application-view recording remains available for a later
user-started trial. To include it, use a new name:

```sh
.venv/bin/python -B -m conductor.app --virtual-bow --backend avfoundation --camera-built-in --seconds 180 --record virtual-bow-01 --record-view app --record-seconds 45
```

Wait for Recording READY, then press C for the three-second countdown. Q ends
the preview and finalizes output. Recording stays local under ignored
`recordings/virtual-bow-01/`, without audio or desktop capture. App-view
diagnostics include the virtual-bow snapshot paired with source sequence,
capture timestamp and render timestamp. The AVI remains at processed-frame
cadence; use the JSONL mapping for elapsed time. No recording is enabled by
default. See [camera/recording details](CAMERA_SELECTION.md).

## Validation and limits

The standard-library motion tests cover slow up, fast down, mirrored horizontal
motion, inversion, reversal, deadband, disagreement, missing/invalid hands,
stale/future/reordered/duplicate timestamps, track changes, pauses, outliers,
bounded speed and both travel limits. A real `ConductorSession` synthetic test
verifies that the adapter reads its tracks without changing detector state.

App-loop tests exercise the separate synthetic mode and pause/resume controls,
verify two-hand model configuration and disabled mock/audio/hold modes, render
both trajectories/speeds in memory, and check recording metadata. Full isolated
regression: **558 tests, zero failures/errors, one Windows-only skip**, with nine
isolation checks and zero forbidden attempts (`logs/virtual-bow-validation.json`).
The 21 focused tests are included in that total. Existing regression fixtures
remain intact. The no-window synthetic CLI also runs without a camera/model.

Live hand reliability, the teacher's exact triangular gesture, useful gain and
deadband calibration, and real recording throughput remain unverified. No
physical cello behavior is implemented or validated by this prototype.
