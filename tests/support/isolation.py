"""Fail-closed boundaries for local synthetic tests, not an OS sandbox."""
from contextlib import ExitStack
from pathlib import Path
import multiprocessing.process
import socket
import subprocess
import sys
from types import ModuleType
from unittest.mock import patch


class HardwareIsolation:
    def __init__(self):
        self.attempts = []
        self.active = False
        self.stack = ExitStack()

    def deny(self, name):
        def blocked(*args, **kwargs):
            self.attempts.append(name)
            raise RuntimeError('Synthetic tests forbid ' + name)
        return blocked

    def audit(self, event, args):
        if not self.active:
            return
        if event == 'open' and isinstance(args[0], (str, bytes)):
            path = Path(args[0].decode() if isinstance(args[0], bytes) else args[0])
            mode = args[1] or ''
            flags = args[2] if len(args) > 2 else 0
            if path.suffix.lower() in {'.avi','.mp4','.wav','.png','.jpg','.jpeg'}:
                if any(c in mode for c in 'wax+') or flags & 0x3:
                    self.deny('media-file writes')()
        if event in {'socket.connect', 'subprocess.Popen', 'os.system'}:
            self.deny(event)()

    def __enter__(self):
        import cv2
        self.stack.enter_context(patch('platform.machine', return_value='synthetic-test-machine'))
        self.active = True
        sys.addaudithook(self.audit)
        for name in ('VideoCapture','VideoWriter','namedWindow','imshow','waitKey',
                     'resizeWindow','getWindowProperty','destroyAllWindows'):
            self.stack.enter_context(patch.object(cv2, name, self.deny('cv2.'+name)))
        audio = ModuleType('sounddevice')
        for name in ('Stream','InputStream','OutputStream','RawStream','RawInputStream',
                     'RawOutputStream','play','rec','playrec','query_devices','query_hostapis'):
            setattr(audio, name, self.deny('sounddevice.'+name))
        cameras = ModuleType('cv2_enumerate_cameras')
        cameras.enumerate_cameras = self.deny('camera enumeration')
        self.stack.enter_context(patch.dict(sys.modules, {'sounddevice':audio,
                                                          'cv2_enumerate_cameras':cameras}))
        self.stack.enter_context(patch.object(multiprocessing.process.BaseProcess,
                                               'start', self.deny('child processes')))
        self.stack.enter_context(patch.object(subprocess, 'Popen', self.deny('subprocesses')))
        # NumPy 1.26's testing module probes CPU capabilities using `lscpu`.
        # Initialize it with an explicit no-capability response, never a process.
        def cpu_probe(command, **kwargs):
            if command != 'lscpu':
                return self.deny('unexpected dependency initialization command')()
            return subprocess.CompletedProcess(command, 0, stdout='', stderr='')
        with patch.object(subprocess, 'run', side_effect=cpu_probe):
            import numpy.testing
        return self

    def self_check(self):
        import cv2
        import sounddevice
        boundaries = [lambda:cv2.VideoCapture(0), lambda:cv2.VideoWriter(),
                      lambda:sounddevice.InputStream(), lambda:sounddevice.OutputStream(),
                      lambda:sounddevice.query_devices(), lambda:cv2.namedWindow('blocked'),
                      lambda:self.audit('open', ('blocked.avi','wb',1)),
                      lambda:self.audit('socket.connect', (None, ('example.invalid',443))),
                      lambda:subprocess.Popen(['blocked'])]
        for call in boundaries:
            try:
                call()
            except RuntimeError:
                pass
            else:
                raise AssertionError('An isolation boundary did not reject a call')
        count = len(self.attempts)
        assert count == len(boundaries)
        self.attempts.clear()
        return count

    def __exit__(self, *args):
        self.active = False
        return self.stack.__exit__(*args)
