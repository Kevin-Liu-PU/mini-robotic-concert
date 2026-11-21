"""Shared camera geometry and numeric-only calibration. No device opens here."""
import json
import math
from pathlib import Path
import sys

CALIBRATION_KEYS = {'schema_version','chin_y','camera_index','camera_backend','capture_width',
                    'capture_height','processing_width','preview_height','mirror_horizontal'}

def camera_backend(cv2=None, platform=None):
    # OpenCV's fixed enum IDs permit stdlib-only launcher preflight. The
    # reader/calibrator still check the installed cv2 constants before open.
    platform = sys.platform if platform is None else platform
    if platform == 'darwin':return (cv2.CAP_AVFOUNDATION if cv2 is not None else 1200), 'AVFoundation'
    if platform == 'win32':return (cv2.CAP_DSHOW if cv2 is not None else 700), 'DirectShow'
    return (cv2.CAP_ANY if cv2 is not None else 0), 'OpenCV automatic'

def configure_camera(cv2, cap, width):
    cap.set(cv2.CAP_PROP_FRAME_WIDTH,width)
    cap.set(cv2.CAP_PROP_FRAME_HEIGHT,round(width*9/16))
    cap.set(cv2.CAP_PROP_FPS,30)
    return cap.set(cv2.CAP_PROP_BUFFERSIZE,1)

def prepare_frame(cv2,image,width):
    height=round(image.shape[0]*width/image.shape[1])
    return cv2.flip(cv2.resize(image,(width,height)),1)

def validate_calibration(data,camera_index,width,backend=None,capture_size=None):
    if not isinstance(data,dict) or set(data)!=CALIBRATION_KEYS:
        raise ValueError('Calibration must contain exactly the numeric schema fields; recalibrate this camera.')
    for key in CALIBRATION_KEYS-{'chin_y'}:
        if type(data[key]) is not int:raise ValueError('Invalid integer calibration field: '+key)
    if type(data['chin_y']) not in (int,float) or not math.isfinite(data['chin_y']) or not 0<data['chin_y']<1:
        raise ValueError('chin_y must be finite and strictly between 0 and 1.')
    if data['schema_version']!=1 or data['mirror_horizontal']!=1:
        raise ValueError('Unsupported calibration version or mirror setting.')
    if data['camera_index']!=camera_index or data['processing_width']!=width:
        raise ValueError('Calibration camera index/processing width does not match this run; recalibrate.')
    if camera_index<0 or not 256<=width<=1920 or data['camera_backend']<0 or min(data['capture_width'],data['capture_height'])<2:
        raise ValueError('Invalid calibration camera geometry.')
    expected_height=round(data['capture_height']*width/data['capture_width'])
    if data['preview_height']!=expected_height:
        raise ValueError('Calibration preview height does not match aspect-preserving processing.')
    if backend is not None and data['camera_backend']!=backend:
        raise ValueError('Calibration camera backend/platform differs; recalibrate on this machine.')
    if capture_size is not None and tuple(capture_size)!=(data['capture_width'],data['capture_height']):
        raise ValueError('Actual camera frame dimensions differ from calibration; stop and recalibrate.')
    return data

def load_calibration(path,camera_index,width):
    path=Path(path)
    if path.stat().st_size>4096:raise ValueError('Calibration JSON is unexpectedly large.')
    return validate_calibration(json.loads(path.read_text(encoding='utf-8')),camera_index,width)

def make_calibration(chin_y,camera_index,backend,capture_width,capture_height,processing_width):
    data=dict(schema_version=1,chin_y=chin_y,camera_index=camera_index,camera_backend=backend,
              capture_width=capture_width,capture_height=capture_height,processing_width=processing_width,
              preview_height=round(capture_height*processing_width/capture_width),mirror_horizontal=1)
    return validate_calibration(data,camera_index,processing_width,backend)
