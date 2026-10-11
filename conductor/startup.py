"""Nonblocking model initialization and explicit recording readiness helpers."""
from threading import Lock, Thread
import traceback


class ResourceStartup:
    """Transfer a loaded resource to the UI, or dispose it if the UI has exited.

    The UI owns the startup deadline. This daemon worker cannot kill a stuck
    native loader, but it keeps Python-waiting initialization off the UI thread.
    Never open a camera here: the UI owns camera creation and cleanup.
    """
    def __init__(self, factory, dispose):
        self.factory = factory
        self.dispose = dispose
        self.lock = Lock()
        self.closed = False
        self.ready = False
        self.value = None
        self.error = None
        self.failure_traceback = None
        self.thread = Thread(target=self._run, daemon=True, name='conductor-model-startup')
        self.thread.start()

    def _run(self):
        try:
            value = self.factory()
        except Exception as exc:
            failure_traceback = traceback.format_exc()
            with self.lock:
                if not self.closed:
                    self.error = f'{type(exc).__name__}: {exc}'
                    self.failure_traceback = failure_traceback
                    self.ready = True
            return
        with self.lock:
            if not self.closed:
                self.value = value
                self.ready = True
                return
        self.dispose(value)

    def take(self):
        with self.lock:
            if not self.ready or self.closed:
                return None, None
            value, error = self.value, self.error
            self.value = self.error = None
            self.ready = False
            return value, error

    def close(self):
        with self.lock:
            self.closed = True
            value, self.value = self.value, None
        if value is not None:
            self.dispose(value)


def camera_fault(stats, initialized):
    """An active startup fallback may wait; a dead/error worker may not."""
    status = stats.get('status', '')
    if (not stats.get('worker_alive', False) or
            status.startswith(('CAMERA ERROR', 'CAMERA UNAVAILABLE', 'CAMERA FRAME SIZE', 'CAMERA CLOSED')) or
            initialized and status.startswith('REOPENING')):
        return 'CAMERA_WORKER_UNAVAILABLE'
    return None


def recording_gate(state, fresh_frame):
    """READY means both a healthy source and an enabled screen experiment."""
    if state['fault']:
        return False, 'Clock fault - Enter after recovery'
    if not state['source_initialized']:
        return False, 'Starting camera/model - please wait'
    if not state['healthy'] or not fresh_frame:
        return False, 'Waiting for fresh camera/model frames'
    if not state['armed']:
        return False, 'Clock stopped - Enter before REC'
    return True, None


def source_status_labels(is_demo, camera_stats, starting=False, fault=None):
    """A missing real camera must never be labeled as synthetic input."""
    if is_demo:
        return 'Synthetic input / no camera', '60 BPM > 120 BPM > pause'
    mode = camera_stats.get('mode', 'Camera not initialized')
    if starting:
        status = ('Startup failed: '+fault if fault else
                  'Starting camera/model - waiting for first valid frame')
    else:
        status = camera_stats.get('status', 'Waiting for camera/model')
    return mode, status
