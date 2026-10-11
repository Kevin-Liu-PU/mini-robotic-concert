# Synthetic tests

After setup, run the complete hardware-isolated suite from the repository root:

```powershell
.\.venv\Scripts\python.exe -B run_tests.py
```

`--pattern test_overlap.py` selects one test file. `--report PATH` optionally
writes an aggregate JSON result. `setup.cmd` runs the complete suite.

## Coverage and fixture provenance

The 493 cases cover regular and changing tempos, 25/60/120/180 BPM boundaries,
jitter, pauses, duplicate timestamps, reset/reacquisition, ambiguous hand
association, startup/fault latches, stop/resume, line edits, clock callbacks,
recording error handling, generic configuration and fake device enumeration.

Fixtures contain no recorded user data. `tests/support/synthetic_fixtures.py`
uses explicit constants and formulas for these cases:

| Input | Contract checked |
| --- | --- |
| Nine scalar association cases | Six supported assignments and three ambiguous/invalid ownership cases. |
| Ten piecewise linear strokes | Four continuous contacts and six reacquired episodes with analytically specified contact times. |
| A five-finger drawing with an offset alternative | Geometry, motion history, ownership, boundary and duplicate-frame checks under adversarial transformations. |
| Clock event sequences | Regular cadence, changes, missing/extra events, pauses and source faults. |
| Generated image/audio arrays | In-memory codec serialization, sample placement and bounded callback behavior. |

Expected contacts are not derived by running the production detector. These
fixtures establish regression contracts, not recorded-data accuracy. Pure
observation adapters, event matching and simulation helpers live in
`tests/support/`. The older threshold configuration in `tests/fixtures/` exists
only for regression comparison; public profiles use 25–180 BPM.

## Hardware isolation

The runner installs fail-closed Python boundaries before importing test modules.
Nine deliberate probes check camera, video writer, input/output audio, device
enumeration, native window, media-file writes, network and subprocess blocking.
The probes are then cleared; unexpected attempts during the suite fail the run.
Tests may replace those boundaries with explicit fakes. Windows processor-name
lookup and NumPy's initialization-only `lscpu` probe are mocked so the test helpers
do not spawn system queries.

These guards cover the known APIs used by this suite; they are not an operating-
system sandbox for arbitrary native code. Direct unittest discovery omits the
extra guards and is not the validated command. No test requests camera,
microphone, location or security permissions. Metadata and lock-contention tests
use temporary directories. Media bytes remain in memory; no recordings are
created. Windows is the validated platform. Native Windows file-lock tests do
not establish Linux/macOS compatibility.
