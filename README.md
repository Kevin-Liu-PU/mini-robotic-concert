# A Mini Robotic Concert: Human Conducting and Multi-Robot Coordination

A Python prototype in which a person conducts and two robots play. A camera
follows the conductor's hand and turns the gesture into a beat clock. A UR5e arm
bows a cello and eight solenoids strike a glockenspiel, both from that clock.
Everything also runs without hardware, with a simulated arm and the strikers
switched off.

Project code is available under the [MIT License](LICENSE), copyright 2026
Zixian Liu. Third-party packages and models retain their own licenses; see
[THIRD_PARTY.md](THIRD_PARTY.md).

## What it does

- `conductor/` reads the conducting pattern from the wrist, in three or in four
  beats to the bar. The lowest point of each downward stroke is a beat, and the
  lift before the next bar is the last beat. A clock holds tempo and bar
  position, corrects them at each detected beat, and predicts the beats to come.
  It publishes the clock as JSON Lines over a loopback TCP connection.
- `conductor_cello/` plays one bow stroke per bar on the open A string of a cello
  with a UR5e. Each stroke is one straight tool motion, planned as the bar
  begins. Force and torque limits, a start-region check and an operator
  confirmation guard every motion.
- `conductor_glock/` strikes each note of the melody when the beat position
  reaches it, and passes the clock on to the cello.

Two pieces are included: *Arirang*, 16 bars in three, and *Twinkle, Twinkle,
Little Star*, 12 bars in four. The notes are fixed. Only their timing comes
from the conductor.

## Demo

[Watch the demo](media/demo.mp4). Both robots play *Arirang* from beats
extracted from a recorded conducting video. The inset shows the conductor with
the detected bar and beat.

## Setup

Use 64-bit Python 3.11. The project was developed and run on macOS; other
platforms have not been tried for this version. From the repository root:

```sh
python3.11 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
```

This covers the camera conductor, both receivers without hardware, and the
tests. The real instruments need two more packages:

```sh
python -m pip install -r requirements-hardware.txt
```

Tracking runs locally with the models that the MediaPipe package provides. No
model file is bundled here.

## Try it without hardware

Use three terminals, in this order. The receivers must be listening before the
conductor starts.

```sh
# 1. simulated arm
python -m conductor_cello play --source live --song arirang --mode air --mock --port 8767

# 2. glockenspiel with the strikers switched off; passes the clock on to the cello
python -m conductor_glock play --song arirang --forward 8767 --dry-run

# 3. the conductor: opens the camera and a window
conductor/Start-Conductor-Three.command
```

Step into the picture and conduct in three. The piece starts after one or two
complete bars and ends by itself after sixteen. The STOP button or Space stops
at any time. For four beats to the bar, use `--song twinkle` in the first two
commands and `conductor/Start-Conductor-Four.command`.

The cello alone can also play from an internal clock:

```sh
python -m conductor_cello play --source metronome --bpm 70 --song arirang --mode air --mock
```

## With the robots

| Command | What it does |
| --- | --- |
| `python -m conductor_cello state` | Read-only snapshot of the arm. Opens no control program. |
| `python -m conductor_cello diag --go` | Sets and checks the tool settings. No motion. |
| `python -m conductor_cello home --go` | Moves to the clear line above the string. |
| `python -m conductor_cello play --source live --song arirang --mode air --go --port 8767` | One bow stroke per bar above the string. |
| `python -m conductor_cello play --source live --song arirang --mode contact --depth-mm 2 --go --port 8767` | The same on the string. |
| `python -m conductor_glock ports` | Lists serial ports. Opens nothing. |
| `python -m conductor_glock check --go` | Handshake with the controller. Strikes nothing. |
| `python -m conductor_glock play --song arirang --forward 8767 --go` | Strikes the notes and passes the clock on. |

Without `--go` nothing is sent to hardware. Every command that moves the arm
asks the operator to type `YES`. If a limit on force or torque is exceeded, the
arm stops, holds its position and accepts no further motion. The emergency stop
of the robot is the only safety-rated stop, and an operator stands at the robot
with it for every run.

The taught bow line, limits and tool settings in `conductor_cello/` belong to
one robot, one cello and one bow mount. They are not valid for another setup.
See [conductor_cello/README.md](conductor_cello/README.md) before using an arm.

## Tests and data

```sh
python -m pytest tests
```

The 159 tests run on a simulated arm with a fake clock, scripted clock streams
and loopback sockets. They open no camera, robot, serial port or audio device.
Two of them need OpenCV and are skipped without it.

No recordings are included apart from `media/demo.mp4`. The conductor records
the raw camera picture and its logs under `conductor/runs/`, and each receiver
writes a run folder with every event under its own `runs/`. All of these stay on
the local machine and are ignored by Git. There is no upload functionality.

## Results and limits

- In three live 16-bar takes at a steady tempo the detector reported 143 of 144
  beats. A beat is confirmed 0.13 to 0.16 s after the gesture, which is why the
  clock predicts.
- From a recorded conducting video both robots played *Arirang* complete: 16 bow
  strokes and 53 notes. Strike commands left at most 3.3 ms after their beats,
  and the commanded onsets of the two instruments at bar lines were a median of
  15 ms apart. These are command times. Acoustic onsets have not been measured.
- Under live conducting the robots follow a steady pulse. Large changes of tempo
  are not yet followed.
- The four-beat piece has run only with the simulated arm and the strikers
  switched off.
- The gesture rules were derived from one conductor's recordings. This is not a
  general recognizer of conducting patterns.
- This is soft real-time Python software, not a calibrated timing system.

## Structure

| Path | Contents |
| --- | --- |
| `conductor/` | Camera tracking, beat detection, the clock, the window, optional local recording. |
| `conductor_cello/` | Bow planning, guarded motion and safety checks for the arm; a simulated arm. |
| `conductor_glock/` | Note scheduling and the relay to the cello; the two note lists. |
| `docs/wire-protocol.md` | The clock messages and what a receiver does with them. |
| `tests/` | Offline tests for the three packages. |
