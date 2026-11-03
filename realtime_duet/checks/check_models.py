"""Check bundled MediaPipe model resources without importing/loading MediaPipe."""
import importlib.metadata
import json
from pathlib import Path

RESOURCES = (
    'holistic_landmark/holistic_landmark_cpu.binarypb',
    'holistic_landmark/hand_recrop.tflite',
    'pose_detection/pose_detection.tflite',
    'pose_landmark/pose_landmark_full.tflite',
    'palm_detection/palm_detection_full.tflite',
    'hand_landmark/hand_landmark_full.tflite',
    'face_detection/face_detection_short_range.tflite',
    'face_landmark/face_landmark.tflite',
)


def main():
    try:
        distribution = importlib.metadata.distribution('mediapipe')
    except importlib.metadata.PackageNotFoundError:
        print('Install requirements-macos.txt into the selected environment first.')
        return 1
    root = Path(distribution.locate_file('mediapipe/modules'))
    missing = [name for name in RESOURCES if not (root / name).is_file() or (root / name).stat().st_size == 0]
    print(json.dumps(dict(mediapipe=distribution.version, missing=missing,
                         models_loaded=False, devices_opened=False), indent=2))
    return 1 if missing or distribution.version != '0.10.21' else 0


if __name__ == '__main__':
    raise SystemExit(main())
