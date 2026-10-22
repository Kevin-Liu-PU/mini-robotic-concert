# One observed gesture → one MOCK bow

This first conductor-to-cello milestone contains **no hardware adapter**. It
uses the existing observed `Beat` returned by `ConductorSession`, never a
predicted metronome, screen-clock or audio tick. The bow is a 0.5-second numeric
deadline, polled on the app thread; it adds no sleeps, workers or action queue.

## No-device proof on Mac

From the repository root with an existing Python 3.10+ interpreter:

```sh
python3 -B -S -m conductor.mock_cello_demo
python3 -B -S -m unittest tests.test_mock_cello tests.test_mock_cello_app -v
```

`-S` disables site packages. These commands use only the standard library. The
demo supplies synthetic fingertip observations to the real detector and app
seam, explicitly scripts A then B once, and advances fake time without sleeping.
Expected transitions: DISARMED → ARMED → BUSY → DISARMED, with one started and
one completed mock bow and zero queued actions. The app-loop tests replace all
device, drawing and time boundaries with fakes; they do not open a window.

## Optional preview integration

Only in a separately prepared conductor environment with its existing GUI
dependencies, the synthetic preview command is:

```sh
python3 -B -m conductor.app --demo --mock-cello
```

The opt-in flag appends a **CELLO MOCK** status strip. Default execution keeps
the existing audio/screen behavior. The original `--demo` preview opens a native
window; the standard-library demo above does not. No real camera run is part of
this milestone's validation.

- While a fresh MAIN fingertip is visible, press **A then B** to authorize one
  new observed beat. Both keys are required for every bow, preventing a held B
  key from repeatedly authorizing new bows.
- **X / Space** stops the mock and cancels its deadline. Hand loss, stale source,
  MAIN identity changes, line edits, Enter, reset and exit clear authorization.
- **Enter** retains its existing clock-resume meaning and never arms the mock.
- **R** explicitly resets a latched mock fault to DISARMED. A new A/B sequence
  and a new post-arm gesture are still required.

## Admission and ordering contract

Startup is DISARMED. Only an ARMED mock accepts a strictly post-arm timestamp,
no more than 250 ms old, at or before delivery time. Duplicate, reordered,
pre-arm, future and invalid timestamps are rejected. The observed timestamp
watermark survives reset/re-arm; `Beat.count` is diagnostic because the detector
restarts it on reset. BUSY drops every arrival, including one delivered on the
completion boundary: events are offered before completion is polled. Completion
disarms, never replays an event, and never authorizes another bow. Stop cancels
only a numeric mock deadline; there is no asynchronous completion callback that
could act on a later arm. Mock start/completion exceptions and invalid monotonic
time latch FAULT. App exceptions also latch FAULT.

App authorization requires a fresh observed MAIN source. A fresh frame after a
processing gap cannot revive an earlier authorization. All state and controls
run on the same application thread. Future concurrency would require a separate
generation/cancellation design; this implementation does not assert that one
already exists.

## Environment and scope

Local Mac verification (Python 3.11.2, site packages disabled): 24 new mock and
fake app-loop tests pass. Together with the existing core, multitrack,
association, screen-clock, dropout, beat-line and health suites, 167 tests pass.
The full `run_tests.py` hardware-isolated suite could not start because this
interpreter lacks `cv2`; no packages were installed. These subset results do not
replace the full suite or validate native preview, MediaPipe or audio on Mac.

The published Windows baseline is `fc3791e99e4f8e039cc50a75c964423dc254829b`.
Later uncommitted Windows changes have not been inspected. Existing configs
default to DirectShow (`dshow`) and launchers are `.cmd`; this change does not
claim that the real camera/audio application has been ported or validated on Mac.
The published conductor validation uses Python 3.10.11 and NumPy 1.26.4. Cello
requires Python >=3.11,<3.13 and NumPy >=2: keep their environments separate.

No robot connection, IPC, score scheduling, tempo/phase synchronization or RL
is implemented. Before a future hardware adapter:

- `recording_a_only.execute_full_stroke` means prepare → full taught bow → lift;
  it is **not automatically a short safe bow**.
- Baseline automatic recovery may unlock protective stops, restart safety,
  power on/release brakes and retry. A hardware adapter must fail closed and
  must never silently inherit those behaviors. Do not call recovery/dashboard
  functions as part of this mock milestone.
- Cancellation, motion limits and completion/fault feedback need separate
  hardware review and explicit physical-execution authorization.
