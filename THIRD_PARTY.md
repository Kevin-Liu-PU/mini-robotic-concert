# Third-party sources and licenses

## Project license

The project's own code is licensed under the [MIT License](LICENSE), copyright
2026 Zixian Liu. Third-party packages and pretrained assets retain their original
licenses and attribution; the project MIT license does not replace their terms.

## Direct dependencies

The version pins are in `requirements-lock.txt`. Their installed distribution
metadata and bundled notices were inspected for the pinned environment.

| Dependency | Version | Source and license |
| --- | --- | --- |
| MediaPipe | 0.10.21 | [Official source](https://github.com/google-ai-edge/mediapipe/tree/v0.10.21), Apache-2.0; package includes an additional notice in its LICENSE. |
| opencv-contrib-python | 4.11.0.86 | [OpenCV packaging](https://github.com/opencv/opencv-python), packaging scripts MIT; [OpenCV](https://github.com/opencv/opencv/tree/4.11.0) Apache-2.0; wheels contain separately licensed components. |
| NumPy | 1.26.4 | [Official source](https://github.com/numpy/numpy/tree/v1.26.4), BSD-3-Clause; binary distributions include additional notices. |
| cv2-enumerate-cameras | 1.4.0 | [Maintainer's PyPI release](https://pypi.org/project/cv2-enumerate-cameras/1.4.0/), MIT. |
| sounddevice | 0.5.6 | [Maintainer's PyPI release](https://pypi.org/project/sounddevice/0.5.6/), MIT. |

`third_party/` preserves the direct packages' license files from the exact
installed distributions. OpenCV's `LICENSE-3RD-PARTY.txt` covers additional
components, including FFmpeg. The [packaging license documentation](https://github.com/opencv/opencv-python#licensing)
identifies FFmpeg under LGPLv2.1. Dependencies, wheels, DLLs, OBS and FFmpeg
executables are not distributed in this repository. A future binary distribution
requires a separate review of its actual bundled components and obligations.
This is not an exhaustive legal review of all transitive dependencies.

## Pretrained model

The application uses Google's Hand Landmarker full float16 bundle, version 1:

- [Official task overview](https://developers.google.com/edge/mediapipe/solutions/vision/hand_landmarker)
- [Versioned model download](https://storage.googleapis.com/mediapipe-models/hand_landmarker/hand_landmarker/float16/1/hand_landmarker.task)
- [Official linked model card](https://storage.googleapis.com/mediapipe-assets/Model%20Card%20Hand%20Tracking%20%28Lite_Full%29%20with%20Fairness%20Oct%202021.pdf), which states Apache License, Version 2.0 on page 2.
- SHA256: `fbc2a30080c3c557093b5ddfc334698132eb341044ccee322ccf8bcf3607cde1`

The model is not included. `download_model.py` downloads from the official source
and rejects mismatched content. The model card discusses training sources,
limitations and intended use; model performance is not a timing guarantee for
this application. Preserve the relevant license/attribution material if the
model is redistributed in a future release.

## Technical references and attribution

- [MediaPipe Hand Landmarker Python guide](https://developers.google.com/edge/mediapipe/solutions/vision/hand_landmarker/python)
- [OpenCV 4.11 capture properties](https://docs.opencv.org/4.11.0/d4/d15/group__videoio__flags__base.html)
- [sounddevice 0.5.6 stream API](https://python-sounddevice.readthedocs.io/en/0.5.6/api/streams.html)

This prototype uses these APIs and pretrained assets; it does not claim to have
trained the landmark models. Existing technical explanations and upstream
license notices are retained. No private recording is distributed as a dataset.
