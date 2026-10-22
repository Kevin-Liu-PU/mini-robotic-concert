# Camera selection on Mac

Camera selection is independent of the MOCK cello. Listing devices reads
metadata only; starting the live app opens the selected stream. Recording and
audio remain opt-in. The current automated validation has opened no stream.

In a separately prepared conductor environment, from the repository root:

```sh
# List native names, indices and built_in classification; no stream is opened.
python -B -m conductor.app --list-cameras --backend avfoundation

# Later live preview: require exactly one system-classified built-in camera.
python -B -m conductor.app --backend avfoundation --camera-built-in --num-hands 1 --mock-cello

# Alternatively select a unique name shown by --list-cameras, or its index.
python -B -m conductor.app --backend avfoundation --camera-name "EXACT NAME FROM LIST" --mock-cello
python -B -m conductor.app --backend avfoundation --camera 1 --mock-cello
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
The full selected subset, including mock cello tests, passes 181 tests with
Python 3.11.2 and `-S` (no site packages):

```sh
python3.11 -B -S -m unittest tests.test_core tests.test_multitrack tests.test_association tests.test_screen_clock tests.test_dropout_holding tests.test_beat_line tests.test_health tests.test_mock_cello tests.test_mock_cello_app tests.test_camera_selection -q
```

On this Mac the available Python 3.11 lacks OpenCV, `cv2_enumerate_cameras` and
`AVFoundation` (PyObjC). The latter is the Mac-specific dependency declared by
the existing [camera-enumeration package](https://github.com/chinaheyu/cv2_enumerate_cameras/blob/v1.4.0/pyproject.toml).
No package/environment was installed or modified. `system_profiler
SPCameraDataType -json` returned an empty device list; this does not establish
that the machine has no camera. The actual built-in name/index, camera access,
native preview and model compatibility are therefore unverified. The full
hardware-isolated regression runner also remains blocked at missing `cv2`.
Prepare the conductor environment separately from the cello environment before
live validation; do not combine their incompatible NumPy requirements.
