"""Read-only environment check; opens no camera, model, socket, or robot."""
import importlib
import importlib.metadata
import json
import platform
import sys

def main():
    report=dict(python=sys.version,executable=sys.executable,system=platform.system(),machine=platform.machine(),
                camera_opened=False,hardware_calls=0,packages={},errors=[],mac_performance_verified=False)
    for module,package in [('cv2','opencv-contrib-python'),('mediapipe','mediapipe'),('numpy','numpy')]:
        try:
            obj=importlib.import_module(module)
            if module == 'cv2':
                versions = {}
                for candidate in ('opencv-contrib-python', 'opencv-python', 'opencv-contrib-python-headless', 'opencv-python-headless'):
                    try: versions[candidate] = importlib.metadata.version(candidate)
                    except importlib.metadata.PackageNotFoundError: pass
                report['packages'].update(versions)
                if len(versions) != 1 or any('headless' in name for name in versions):
                    report['errors'].append('Use exactly one GUI OpenCV distribution; the pinned environment uses opencv-contrib-python.')
            else:
                report['packages'][package]=importlib.metadata.version(package)
            if module=='mediapipe' and not hasattr(getattr(obj,'solutions',None),'holistic'):
                report['errors'].append('Installed MediaPipe has no solutions.holistic API; select a compatible existing environment.')
        except Exception as exc:report['errors'].append(module+': '+str(exc))
    if sys.version_info<(3,10):report['errors'].append('Python 3.10 or later is required.')
    print(json.dumps(report,indent=2));return 1 if report['errors'] else 0

if __name__=='__main__':raise SystemExit(main())
