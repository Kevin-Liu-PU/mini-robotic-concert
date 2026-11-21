"""Per-thread high-resolution waits; never changes global Windows timer settings."""
import os
import threading
import time

_local = threading.local()


class _Timer:
    def __init__(self):
        import ctypes
        from ctypes import wintypes
        self.ctypes = ctypes
        self.kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        self.kernel.CreateWaitableTimerExW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD]
        self.kernel.CreateWaitableTimerExW.restype = wintypes.HANDLE
        self.kernel.SetWaitableTimer.argtypes = [wintypes.HANDLE, ctypes.POINTER(ctypes.c_longlong), wintypes.LONG, ctypes.c_void_p, ctypes.c_void_p, wintypes.BOOL]
        self.kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        self.kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        # CREATE_WAITABLE_TIMER_HIGH_RESOLUTION; private handle, no timeBeginPeriod.
        self.handle = self.kernel.CreateWaitableTimerExW(None, None, 2, 0x1F0003)
        if not self.handle:
            raise OSError(ctypes.get_last_error(), "high-resolution timer unavailable")

    def wait(self, seconds):
        due = self.ctypes.c_longlong(-max(1, round(seconds*10_000_000)))
        if not self.kernel.SetWaitableTimer(self.handle, self.ctypes.byref(due), 0, None, None, False):
            raise OSError(self.ctypes.get_last_error(), "SetWaitableTimer failed")
        self.kernel.WaitForSingleObject(self.handle, 0xFFFFFFFF)

    def __del__(self):
        if getattr(self, "handle", None):
            self.kernel.CloseHandle(self.handle)


def precise_wait(seconds):
    if seconds <= 0:
        return
    if os.name != "nt":
        time.sleep(seconds)
        return
    if not hasattr(_local, "timer"):
        try:
            _local.timer = _Timer()
        except OSError:
            _local.timer = None
    if _local.timer:
        _local.timer.wait(seconds)
    else:
        time.sleep(seconds)
