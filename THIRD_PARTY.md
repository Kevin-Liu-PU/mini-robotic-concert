# Third-party sources and licenses

## Project license

The project's own code is licensed under the [MIT License](LICENSE), copyright
2026 Zixian Liu. Third-party packages and pretrained assets retain their original
licenses and attribution; the project MIT license does not replace their terms.

## Direct dependencies

The versions are pinned in `requirements.txt` and `requirements-hardware.txt`.

| Dependency | Version | Source and license |
| --- | --- | --- |
| MediaPipe | 0.10.21 | [Official source](https://github.com/google-ai-edge/mediapipe/tree/v0.10.21), Apache-2.0; the package includes an additional notice in its LICENSE. |
| opencv-contrib-python | 4.11.0.86 | [OpenCV packaging](https://github.com/opencv/opencv-python), packaging scripts MIT; [OpenCV](https://github.com/opencv/opencv/tree/4.11.0) Apache-2.0; wheels contain separately licensed components. |
| NumPy | 1.26.4 | [Official source](https://github.com/numpy/numpy/tree/v1.26.4), BSD-3-Clause; binary distributions include additional notices. |
| sounddevice | 0.5.6 | [Maintainer's PyPI release](https://pypi.org/project/sounddevice/0.5.6/), MIT. Used only for the optional beat sound. |
| ur_rtde | 1.6.5 | [Official source](https://gitlab.com/sdurobotics/ur_rtde), MIT. Used only with the real arm. |
| pyserial | 3.5 | [Official source](https://github.com/pyserial/pyserial), BSD-3-Clause per its package metadata. Used only with the real solenoid controller. |
| pytest | unpinned | [Official source](https://github.com/pytest-dev/pytest), MIT. Used only for the tests. |

`third_party/` preserves license files of direct packages as found in the
installed distributions. The pyserial distribution carries no license file; its
license text is in the upstream repository. OpenCV's `LICENSE-3RD-PARTY.txt`
covers additional components, including FFmpeg. Dependencies, wheels and
executables are not distributed in this repository. This is not an exhaustive
legal review of all transitive dependencies.

## Pretrained models

The conductor uses the Holistic solution of the MediaPipe package with the pose
and hand models that the package provides. No model file is included in this
repository.

## Instrument firmware

The glockenspiel's solenoid controller and its firmware were built by teammates
and are not part of this repository. `conductor_glock/songs/` holds the two note
lists that the instrument plays.
