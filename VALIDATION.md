# Validation and limitations

The initial public version was checked on Windows with 64-bit Python 3.10.11.
All 27 packages in `requirements-lock.txt` installed in a separate environment
from official PyPI distributions, and `pip check` found no broken requirements.

The hardware-isolated suite passes **493 tests**, with zero failures, errors or
skips. Nine guard self-checks verify the isolation boundaries. The official
MediaPipe model download was verified against the SHA256 documented in the
README. Dependencies and model weights are not included in this repository.

Tests use synthetic trajectories, authored full-hand geometry, fake devices and
in-memory numerical audio buffers. Recording lifecycle checks use fake writers;
AVI/JPEG serialization checks keep media bytes in memory. See [TESTING.md](TESTING.md)
for commands and the coverage boundaries. No camera, microphone, speaker,
recording or native UI was activated during this release validation.

The following remain unverified for this public version:

- Live camera selection and hand-tracking quality on individual devices.
- Speaker compatibility, perceived click timing and measured acoustic alignment.
- File-backed video-decoder interoperability and non-Windows platforms.
- Physical instrument control; a robotic cello and glockenspiel are future goals.

Responsive tempo following updates from valid observed intervals. It can be
misled by missed contacts or irregular gestures. Basic visual tempo estimation
is distinct from experimental phase correction, and neither demonstrates
calibrated acoustic synchronization. This is a soft real-time prototype.
