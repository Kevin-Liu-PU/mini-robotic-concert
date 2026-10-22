"""Metadata-only camera selection; discovery never opens a capture stream."""
import sys


def camera_backend(configured, override=None):
    backend = override or configured
    if sys.platform == 'darwin':
        if override in ('dshow', 'msmf'):
            raise ValueError('DirectShow/MSMF are Windows-only; use --backend avfoundation on Mac')
        if backend in ('dshow', 'msmf', 'auto'):
            backend = 'avfoundation'
    return backend


def backend_id(backend):
    import cv2
    return getattr(cv2, {'dshow':'CAP_DSHOW', 'msmf':'CAP_MSMF',
                         'auto':'CAP_ANY', 'avfoundation':'CAP_AVFOUNDATION'}[backend])


def _mac_devices():
    # Match the locked OpenCV 4.11 AVFoundation backend's video+muxed list,
    # sorted by uniqueID. Device type identifies built-in, never index or name.
    try:
        import AVFoundation as av
    except ImportError as exc:
        raise ImportError('Mac camera discovery needs pyobjc-framework-AVFoundation '
                          'in the separate conductor environment') from exc
    native = list(av.AVCaptureDevice.devicesWithMediaType_(av.AVMediaTypeVideo))
    native += list(av.AVCaptureDevice.devicesWithMediaType_(av.AVMediaTypeMuxed))
    native.sort(key=lambda device: str(device.uniqueID()))
    builtin_type = getattr(av, 'AVCaptureDeviceTypeBuiltInWideAngleCamera', None)
    return [dict(index=i, name=str(device.localizedName()), backend='avfoundation',
                 id=str(device.uniqueID()), vid=None, pid=None,
                 built_in=bool(builtin_type is not None and hasattr(device, 'deviceType')
                               and device.deviceType() == builtin_type))
            for i, device in enumerate(native)]


def list_devices(backend="dshow"):
    if backend == 'avfoundation':
        return _mac_devices()
    from cv2_enumerate_cameras import enumerate_cameras
    return [{"index":item.index,"name":item.name,"backend":backend,
             "vid":f"{item.vid:04X}" if item.vid is not None else None,
             "pid":f"{item.pid:04X}" if item.pid is not None else None}
            for item in enumerate_cameras(backend_id(backend))]


def resolve_camera(cfg, name=None, *, built_in=False):
    devices = list_devices(cfg["camera_backend"])
    if name or built_in:
        matches = ([d for d in devices if d.get('built_in')] if built_in else
                   [d for d in devices if name.casefold() in d["name"].casefold()])
        if len(matches) != 1:
            selection = 'built-in camera' if built_in else f'Camera name {name!r}'
            raise ValueError(f"{selection} matched {len(matches)} devices; use --list-cameras")
        cfg["camera_index"] = matches[0]["index"]
    if cfg['camera_backend'] == 'avfoundation':
        selected = [d for d in devices if d['index'] == cfg['camera_index']]
        if len(selected) != 1 or not selected[0].get('id'):
            raise ValueError('Mac camera index/identity unresolved; use --list-cameras')
        cfg['camera_id'] = selected[0]['id']
    cfg["camera_name"] = next((d["name"] for d in devices if d["index"] == cfg["camera_index"]),"Unresolved camera")
    return cfg


def verify_mac_camera(cfg):
    """Before each open/retry, fail if the selected index now names another device."""
    matches = [d for d in list_devices('avfoundation')
               if d['index'] == cfg['camera_index'] and d.get('id') == cfg.get('camera_id')]
    if not cfg.get('camera_id') or len(matches) != 1:
        raise ValueError('Selected Mac camera disappeared or changed index; select again with --list-cameras')
