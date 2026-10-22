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

`system_profiler SPCameraDataType -json` returned an empty device list before
setup; this does not establish that the machine has no camera. No real
AVFoundation enumeration or capture was run after installation. The actual
built-in identity, camera access, native preview and MediaPipe model
initialization remain unverified. The model asset is not downloaded by pip;
`models/hand_landmarker.task` is still absent. The existing `download_model.py`
fetches it from Google's official bucket with a pinned SHA256 check; downloading
or initializing that asset was outside this dependency-only validation.
The standard-library demo is ready now. Keep the conductor and cello environments
separate because their NumPy requirements are incompatible.
