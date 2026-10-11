"""Name/index discovery using native DirectShow or Media Foundation enumeration."""
import cv2
from cv2_enumerate_cameras import enumerate_cameras

BACKENDS = {"dshow":cv2.CAP_DSHOW,"msmf":cv2.CAP_MSMF,"auto":cv2.CAP_ANY}


def list_devices(backend="dshow"):
    return [{"index":item.index,"name":item.name,"backend":backend,
             "vid":f"{item.vid:04X}" if item.vid is not None else None,
             "pid":f"{item.pid:04X}" if item.pid is not None else None}
            for item in enumerate_cameras(BACKENDS[backend])]


def resolve_camera(cfg, name=None):
    devices = list_devices(cfg["camera_backend"])
    if name:
        matches = [d for d in devices if name.casefold() in d["name"].casefold()]
        if len(matches) != 1:
            raise ValueError(f"Camera name {name!r} matched {len(matches)} devices; use --list-cameras")
        cfg["camera_index"] = matches[0]["index"]
    cfg["camera_name"] = next((d["name"] for d in devices if d["index"] == cfg["camera_index"]),"Unresolved camera")
    return cfg
