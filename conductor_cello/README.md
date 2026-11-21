# conductor_cello

The robot side: a UR5e bows the open A string of a cello, one stroke per bar,
from the conductor's clock.

## What it does

- One straight tool motion (`moveL`) per stroke, at constant speed inside the
  stroke. Length and speed are chosen as the bar begins so that the stroke ends
  on the predicted bar line. Successive strokes stay centred on the bow.
- Before playing, the arm sets the bow on the string and checks that the contact
  force lies inside an accepted band. Otherwise it lifts off and does not play.
- `--mock` replaces the arm with a built-in simulated one, on the same code path.

## Commands

| Command | What it does |
| --- | --- |
| `python -m conductor_cello state` | Read-only snapshot. Opens no control program. |
| `python -m conductor_cello diag --go` | Opens the control interface, sets and checks the tool settings. No motion. |
| `python -m conductor_cello home --go` | From a nearby pose: up, across, and down to the clear line above the string. |
| `python -m conductor_cello goto --u 0.5 --go` | To the clear line above bow fraction `u`. |
| `python -m conductor_cello bow --mode air --go` | One guarded stroke, for checking timing and stopping. |
| `python -m conductor_cello play --source live --song arirang --mode air --go` | The piece above the string, from the conductor's clock. |
| `python -m conductor_cello play --source live --song arirang --mode contact --depth-mm 2 --go` | The same on the string. |

`--source metronome --bpm 70` plays from an internal clock and
`--source replay --stream FILE` from a recorded clock. Without `--go` (or `--mock`) nothing is sent to the robot.
Every command that moves the arm asks the operator to type `YES`.

## Safety

- The arm moves only along straight segments between points of the taught bow
  line, at no more than 0.25 m/s.
- Force and torque are judged as changes from a reading taken at rest. A change
  above the limit (10 N on the string, 7 N in the air, 1.8 N m) stops the arm.
  It then holds its position and accepts no further motion.
- A programmed move may begin only inside a start region around the taught line.
- Tool settings are written once per session and confirmed by read-back.
- The software never unlocks a protective stop, never powers the arm on, and
  never reconnects by itself.
- The emergency stop of the robot is the only safety-rated stop. An operator
  stands at the robot with it for every run.

The taught points, limits and tool settings in `geometry.py` and `executor.py`
belong to one robot, one cello and one bow mount. They are not valid elsewhere.

## Files

| File | Purpose |
| --- | --- |
| `geometry.py` | Taught bow line and its envelopes. No robot access. |
| `bowplan.py` | Length and speed of each stroke. |
| `clock.py`, `wire.py` | Receiving and extrapolating the conductor's clock. |
| `executor.py` | Guarded moves, limits, the playing loop. |
| `homing.py` | The path back to the start region. |
| `driver.py` | The real port (ur_rtde) and the simulated one. |

Each run writes a folder under `conductor_cello/runs/` with every event and a
summary. The messages are described in [docs/wire-protocol.md](../docs/wire-protocol.md).
