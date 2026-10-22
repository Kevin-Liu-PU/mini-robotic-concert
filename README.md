# Mini Robotic Concert

A local Python prototype that turns visible hand gestures into detected beats,
tempo estimates and an optional experimental audio metronome. It uses pretrained
MediaPipe hand landmarks and OpenCV. Connecting a human conductor to a robotic
cello and glockenspiel is a future goal; no robot interface is included.

Project code is available under the [MIT License](LICENSE), copyright 2026
Zixian Liu. Third-party packages and models retain their own licenses; see
[THIRD_PARTY.md](THIRD_PARTY.md).

## Setup on Windows

Install **64-bit CPython 3.10** with the Windows Python launcher. Python 3.10.11
is the validated interpreter. Clone this repository with Git, then run setup
from its folder in PowerShell:

```powershell
git clone https://github.com/Kevin-Liu-PU/mini-robotic-concert.git
cd mini-robotic-concert
.\setup.cmd
```

Setup creates `.venv`, installs `requirements-lock.txt` from official PyPI,
checks dependency consistency, and runs the hardware-isolated synthetic tests.
It does not start the application or download the model. Download the official
model before using a camera:

```powershell
.\.venv\Scripts\python.exe download_model.py
```

The version-1 model is verified against SHA256
`fbc2a30080c3c557093b5ddfc334698132eb341044ccee322ccf8bcf3607cde1`.
Network access is needed for installation and this explicit download. Tracking
then runs locally. Packages, models and virtual environments are not bundled.

## Choose a mode

All public profiles support **25–180 BPM**. One prepared downward contact with
the visible horizontal line represents one quarter note. The tracked beat point
is **index fingertip 8**; wrist 0 helps associate observations over time.

| Command | Behavior |
| --- | --- |
| `.\demo.cmd` | Synthetic 60/120 BPM display and pauses; no camera, model or sound. |
| `.\demo-hold.cmd` | Synthetic screen-only hold-clock example. |
| `.\launch-single-hand.cmd` | One-hand camera view and robust measured tempo; free-running visual metronome. |
| `.\launch-hold-single-hand.cmd` | Conservative screen clock that holds tempo during normal hand loss. |
| `.\launch-responsive.cmd` | Recommended responsive screen-only profile; one-hand camera input. |
| `.\launch-audio-single-hand.cmd` | Same responsive profile with output-only audio, initially muted. |
| `.\launch.cmd` | Two-hand view; one selected MAIN supplies tempo. |

Responsive mode establishes tempo from two reliable contacts and updates it
from each valid observed interval. It can react quickly, but an unobserved beat
or an irregular gesture can produce a wrong tempo. The base detector also shows
a robust rolling interval estimate; that estimate and the responsive clock are
different outputs. A tempo estimate is not evidence of phase synchronization.
The basic visual oscillator is free-running. Hold/audio modes experimentally
adjust future prediction phase, including return from hand loss. Host audio
timestamps are estimates; acoustic alignment and latency are not established.

## Select devices

Camera and audio profiles contain no personal device names. Camera index 0 and
Windows DirectShow are the defaults; indices may change after reconnection.
List available camera names without opening streams, then choose an index or
unique name for the desired mode:

```powershell
.\launch.cmd --list-cameras
.\launch-responsive.cmd --camera 1
.\launch-responsive.cmd --camera-name 'Your camera name'
```

`--backend msmf` selects Media Foundation if DirectShow is unsuitable. Width,
height and requested FPS are editable in JSON; negotiated settings can differ.

Audio uses the system default output unless configured. To inspect output-capable
devices (enumeration only; no playback):

```powershell
.\.venv\Scripts\python.exe tools\list_audio_devices.py
```

Set `device` to a listed index and `device_name_contains` to its name in
`config-audio.json` to guard against an index changing to another device. Leave
both `null` to use the system default. The audio launcher is explicit opt-in;
press **M** to unmute after confirming the intended output and a suitable volume.
No microphone stream is used.

## Controls

| Key | Action |
| --- | --- |
| Q / Esc | Quit and release resources. |
| W / S | Raise/lower the beat line; clear stale detection evidence. |
| R | Reset tracking and tempo evidence. |
| 1 / 2 | Choose MAIN A/B in two-hand mode; stop hold-clock output. |
| Space | Stop hold/audio predictions; detection may remain visible. |
| Enter | Re-arm hold/audio clock; an available saved tempo can resume. |
| M | Toggle audio mute, only in audio mode. |
| C | Start the countdown only when recording was explicitly enabled. |

The focused OpenCV window receives keys. For a first camera trial, show one hand,
wait for MAIN/READY, then return above the preparation threshold between downward
contacts. Single-hand model mode cannot guarantee physical identity if a second
hand enters. Separate ambiguous hands and reset when needed.

## Tests and data

```powershell
.\.venv\Scripts\python.exe -B run_tests.py
```

This runner blocks camera/audio access, native windows, child processes, network
connections and media-file writes. Numerical audio buffers are never played;
codec tests serialize generated frames in memory. Temporary JSON metadata is
removed after tests. The suite needs NumPy/OpenCV, but no camera, microphone,
speaker, model or recorded input. See [TESTING.md](TESTING.md) for test provenance
and the limits of the Python-level isolation boundaries.

No recordings, screenshots, personal paths or real-user fixtures are included.
Normal video frames stay in RAM. Audio mode writes local numeric diagnostics to
`logs/`. `--report PATH` writes numeric diagnostics. The existing optional
`--record NAME` feature can save up to 45 seconds of silent camera video locally
after C/countdown; it is disabled by default and was tested only with mocks or
in-memory data for this version. No upload functionality is provided.

## Limits and structure

Lighting, occlusion, motion blur, sampling rate and tracking interruptions can
cause missed or wrong beats. A/B are geometric track segments, not biometric
identities. Confidence/consistency indicators are heuristics. The gesture is an
interface convention, not a universal conducting pattern recognizer. This is
soft real-time Python software, not a calibrated timing system.

`conductor/` contains tracking, detection, UI, clocks and optional recording.
`config*.json` contains editable profiles. `tests/support/` contains pure helpers
and explicitly synthetic fixtures. The configuration under `tests/fixtures/` is
an older threshold profile used only for regression comparison. No historical
recording or frozen duplicate implementation is required.

The synthetic suite checks numerical behavior and control-state contracts. It
does not establish real-world conducting accuracy, speaker latency or acoustic
synchronization. Live camera and audible-output behavior have not been validated
for this published version. See [VALIDATION.md](VALIDATION.md) for the validation
scope. Physical instrument integration remains future work.

## Mock cello milestone (opt-in)

For a standard-library-only Mac/Windows proof with no devices, run
`python3 -B -S -m conductor.mock_cello_demo` from this directory.
See [MOCK_CELLO.md](MOCK_CELLO.md) for single-bow controls, tests and limitations.

Mac camera selection supports `--camera-built-in`, `--camera-name` and `--camera`
with `--backend avfoundation`. `./launch-responsive-mac.command --mock-cello`
selects the same responsive screen/hold profile as the recommended Windows
launcher, with built-in camera. See [CAMERA_SELECTION.md](CAMERA_SELECTION.md)
for metadata-only listing, launch commands and current Mac validation limits.
