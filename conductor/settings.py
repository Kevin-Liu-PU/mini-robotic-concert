import json
from pathlib import Path
from .core import DetectorConfig
from .tracking import AssociationConfig

ROOT = Path(__file__).resolve().parent.parent


def load_config(path=None):
    config_path = Path(path).resolve() if path else ROOT / "config.json"
    cfg = json.loads(config_path.read_text(encoding="utf-8-sig"))
    DetectorConfig(**cfg["detector"])
    if cfg.get('follow_policy') == 'responsive':
        from .screen_clock import ScreenClockConfig
        clock = ScreenClockConfig(**cfg.get('screen_clock', {}))
        detector = DetectorConfig(**cfg['detector'])
        if (clock.min_bpm, clock.max_bpm) != (detector.min_bpm, detector.max_bpm):
            raise ValueError('Responsive detector and screen clock BPM ranges must match')
        if detector.stale_min_s < 60/clock.min_bpm+detector.max_frame_gap_s:
            raise ValueError('Responsive stale_min_s must cover the slowest interval plus one frame gap')
    AssociationConfig(**cfg.get("association",{}))
    cfg.setdefault('num_hands',2)
    if type(cfg['num_hands']) is not int or cfg['num_hands'] not in (1,2):
        raise ValueError('num_hands must be 1 (show only one hand) or 2')
    if cfg["hand"] not in ("any", "Left", "Right"):
        raise ValueError("hand must be any, Left or Right")
    if cfg["camera_backend"] not in ("dshow", "msmf", "auto", "avfoundation"):
        raise ValueError("camera_backend must be dshow, msmf, auto or avfoundation")
    for key in ("width", "height", "requested_fps", "inference_width"):
        if not isinstance(cfg[key], int) or cfg[key] <= 0:
            raise ValueError(f"{key} must be a positive integer")
    if not isinstance(cfg["camera_index"], int) or cfg["camera_index"] < 0:
        raise ValueError("camera_index must be a nonnegative integer")
    for key in ("min_hand_detection_confidence", "min_hand_presence_confidence", "min_tracking_confidence"):
        if not 0 <= cfg[key] <= 1:
            raise ValueError(f"{key} must be in [0,1]")
    if not isinstance(cfg["mirror"], bool) or not 0 < cfg["trajectory_seconds"] <= 30:
        raise ValueError("mirror must be boolean; trajectory_seconds must be in (0,30]")
    cfg["model_path"] = str((config_path.parent / cfg["model_path"]).resolve())
    return cfg
