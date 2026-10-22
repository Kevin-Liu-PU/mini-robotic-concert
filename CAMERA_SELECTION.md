# Camera selection on Mac

Camera selection is independent of the MOCK cello. Listing devices reads
metadata only; starting the live app opens the selected stream. Recording and
audio remain opt-in. The current automated validation has opened no stream.

The project-local `.venv` is prepared on this Mac. From the repository root:

```sh
# List native names, indices and built_in classification; no stream is opened.
.venv/bin/python -B -m conductor.app --list-cameras --backend avfoundation

# Later live preview, after the model asset is available: require built-in.
.venv/bin/python -B -m conductor.app --backend avfoundation --camera-built-in --num-hands 1 --mock-cello

# Alternatively select a unique name shown by --list-cameras, or its index.
.venv/bin/python -B -m conductor.app --backend avfoundation --camera-name "EXACT NAME FROM LIST" --mock-cello
.venv/bin/python -B -m conductor.app --backend avfoundation --camera 1 --mock-cello
```

The name and index examples must be replaced with verified listing values.
Index 0 is never assumed to mean built-in. `--camera-built-in` uses AVFoundation
device type; a misleading name on a USB camera cannot qualify it. Zero or
multiple built-in devices are an error. Missing/ambiguous names and unknown Mac
indices also fail before opening a stream.

On Mac, the shipped Windows `dshow`/`msmf` defaults and `auto` resolve to
`avfoundation`; an explicit `--backend dshow` or `msmf` is rejected. With no
custom config or camera selection, Mac defaults to built-in selection. A custom
config retains its explicit index/name; add `--camera-built-in` to override it.
Windows defaults, named selection, backend selection and capture format behavior
remain unchanged.

The selected native identity is retained internally. Before every Mac capture
open/retry, the app verifies that the same index still refers to that identity.
If the device disappears or its index changes, capture stops and requires new
selection; it never tries another physical camera. Avoid hot-plugging during a
run: OpenCV opens by index, so a device-list change between the final metadata
check and native open remains a limitation. Width/height/FPS requests still
apply; Mac uses native pixel-format negotiation instead of the Windows FOURCC
request. Existing retries only retry the selected device.

The metadata index order follows the pinned OpenCV 4.11 implementation: video
and muxed devices combined, sorted by unique ID. See the
[OpenCV 4.11 AVFoundation source](https://github.com/opencv/opencv/blob/4.11.0/modules/videoio/src/cap_avfoundation_mac.mm).
This is intentionally the same inventory as that capture backend, rather than a
different discovery list with potentially different indices. Built-in detection
requires the native device-type API; missing classification fails closed.

## Local validation and remaining setup

Fourteen standard-library tests mock platform metadata, built-in classification,
name/index selection, identity changes, Windows behavior, CLI parsing and the
actual app's listing/selection boundaries. No camera/native GUI/audio is used.
The selected standard-library subset, including mock cello tests, passes 182 tests with
Python 3.11.2 and `-S` (no site packages):

```sh
python3.11 -B -S -m unittest tests.test_core tests.test_multitrack tests.test_association tests.test_screen_clock tests.test_dropout_holding tests.test_beat_line tests.test_health tests.test_mock_cello tests.test_mock_cello_app tests.test_camera_selection -q
```

The initial missing-dependency blocker was resolved after explicit installation
approval. This Mac runs arm64 macOS 27.0.1 with existing CPython 3.11.2. All 27
published conductor pins installed from official PyPI wheels into `.venv`;
`requirements-lock.txt` and system Python remain unchanged. The additive
`requirements-macos-lock.txt` pins six additional PyObjC components to 12.2.2:
core, AVFoundation, Cocoa, CoreAudio, CoreMedia and Quartz. The latter components
support the Mac-specific dependency declared by the existing
[camera-enumeration package](https://github.com/chinaheyu/cv2_enumerate_cameras/blob/v1.4.0/pyproject.toml).
`pip check` reports no broken requirements. `.venv/pyvenv.cfg` has
`include-system-site-packages = false`; the cello environment was not changed.

Reproduction commands (all installation targets stay in this project):

```sh
python3.11 -m venv .venv
.venv/bin/python -m pip --isolated --disable-pip-version-check install --no-cache-dir --only-binary=:all: --index-url https://pypi.org/simple -r requirements-macos-lock.txt
.venv/bin/python -m pip --isolated --no-cache-dir check
mkdir -p logs
.venv/bin/python -B run_tests.py --report logs/macos-validation.json
.venv/bin/python -B -S -m conductor.mock_cello_demo
```

The complete hardware-isolated suite executed **532 tests: zero failures/errors,
one skip** (`test_actual_windows_read_handle_does_not_abort_writer`, Windows
sharing mode only). All nine isolation self-checks passed; forbidden attempts
were empty. Results are in local `logs/review-after-fix.json` and its `.log`.
The focused reviewer also verified that internal detector resets invalidate
armed and pending mock authorization, using a real-session synthetic regression.

The later user-approved live test downloaded the official model with the existing
`download_model.py`; SHA256 matched
`fbc2a30080c3c557093b5ddfc334698132eb341044ccee322ccf8bcf3607cde1`.
The project-local ignored asset is `models/hand_landmarker.task`.

Sandboxed metadata enumeration returned an empty list. Authorized native
enumeration outside that sandbox identified **FaceTime HD Camera**, index 0,
with `built_in=true`; an iPhone camera at index 1 was classified as external and
not selected. This confirms index 0 only for the observed session, not as a
portable built-in default. The first attempt requested macOS permission and
read zero frames; it exited cleanly. Before the requested reopen, macOS reported
camera authorization status 3 (authorized).

The reopened 180-second run loaded the real MediaPipe model and processed live
frames in the native preview loop. Numeric diagnostics reported fresh
1920x1080 frames at approximately 30 capture FPS. These observations establish
model load, device selection and preview processing; they alone do not establish
a user gesture-to-mock trigger. Session diagnostics are local numeric-only
`logs/live-mock-camera-2.json` and related JSONL traces. Recording/audio were
disabled; no image/video files or robot connections were used. Keep conductor
and cello environments separate because their NumPy requirements conflict.

The session ended via Q after 152.29 seconds and reported
`camera_closed_cleanly=true`. It processed 3,459 frames, including 428 with the
selected hand, and detected 21 actual downward-contact beats. Mock counters were
`started=0`, `completed=0`, `dropped=21`, `queued=0`; final state was DISARMED
without a fault. Therefore **live hand detection is verified, live one-shot bow
trigger/completion is not yet verified**. Aggregate diagnostics cannot establish
whether A/B was never confirmed or authorization was cleared before a beat.
Do not infer successful mock execution from detector beat counts alone.
