"""Isolated latest-frame capture. Images stay in local shared memory only."""
from collections import deque
import hashlib
import multiprocessing
import time
from .health import FrameFreshness

SEQ, STAMP, HEIGHT, WIDTH, READS, DUPLICATES, FPS, OVERALL_FPS, DRIVER_FPS, STATE, LAST_UNIQUE, ATTEMPT, LAST_READ, FORMAT = range(14)
STATUS = {0:"OPENING CAMERA",1:"CAMERA STREAMING - FRESH FRAMES",2:"REOPENING CAMERA / TRYING FALLBACK",
          3:"CAMERA UNAVAILABLE - CHECK CONNECTION",4:"CAMERA STALLED - PRESS CAMERA KEY TO RETRY",
          5:"CAMERA FRAME SIZE EXCEEDS BUFFER",6:"CAMERA ERROR - CHECK CONSOLE",7:"CAMERA CLOSED"}


def rate(timestamps):
    return ((len(timestamps)-1)/(timestamps[-1]-timestamps[0])
            if len(timestamps)>1 and timestamps[-1]>timestamps[0] else 0.0)


def open_selected_camera(cfg):
    import cv2
    from .devices import backend_id, verify_mac_camera
    if cfg['camera_backend'] == 'avfoundation':
        verify_mac_camera(cfg)
    return cv2.VideoCapture(cfg['camera_index'], backend_id(cfg['camera_backend']))


def _capture_worker(cfg, pixels, metadata, stop_event):
    # DirectShow opens on this PROCESS's main thread, without MediaPipe/UI.
    import cv2
    import numpy as np
    cv2.setNumThreads(1)
    native_format = cfg['camera_backend'] == 'avfoundation'
    target=np.frombuffer(pixels,dtype=np.uint8)
    times=deque(maxlen=180)
    reads=duplicates=sequence=0
    first_time=None
    requested=(cfg["width"],cfg["height"],cfg["requested_fps"],"MJPG")
    # Retry the requested mode once, then lower bandwidth. Release every handle
    # before the next attempt. Frozen successful reads also trigger a retry.
    modes=[requested,requested]
    modes.extend(m for m in ((640,480,30,"YUY2"),(1280,720,30,"MJPG")) if m!=requested)
    cap=None
    try:
        for attempt,(width,height,fps,fourcc) in enumerate(modes,1):
            if stop_event.is_set():
                break
            times.clear()
            with metadata.get_lock():
                metadata[STATE]=0 if attempt==1 else 2
                metadata[ATTEMPT]=attempt
                metadata[FPS]=0
                metadata[FORMAT]=2 if native_format else (0 if fourcc=="MJPG" else 1)
            cap=open_selected_camera(cfg)
            if not cap.isOpened():
                with metadata.get_lock():
                    metadata[STATE]=3
                return
            if not native_format:
                cap.set(cv2.CAP_PROP_FOURCC,cv2.VideoWriter_fourcc(*fourcc))
            cap.set(cv2.CAP_PROP_FRAME_WIDTH,width)
            cap.set(cv2.CAP_PROP_FRAME_HEIGHT,height)
            cap.set(cv2.CAP_PROP_FPS,fps)
            driver_fps=cap.get(cv2.CAP_PROP_FPS)
            freshness=FrameFreshness()
            opened_at=time.perf_counter()
            failures=0
            while not stop_event.is_set():
                ok,frame=cap.read()
                stamp=time.perf_counter_ns()/1e9
                if not ok or frame is None:
                    failures+=1
                    if failures>=10 or stamp-opened_at>2:
                        break
                    stop_event.wait(0.03)
                    continue
                failures=0
                if frame.nbytes>len(target):
                    with metadata.get_lock():
                        metadata[STATE]=5
                    return
                reads+=1
                first_time=stamp if first_time is None else first_time
                times.append(stamp)
                digest=hashlib.blake2b(memoryview(frame),digest_size=16).digest()
                unique=freshness.observe(digest,stamp)
                if not unique:
                    duplicates+=1
                with metadata.get_lock():
                    metadata[READS]=reads
                    metadata[DUPLICATES]=duplicates
                    metadata[FPS]=rate(times)
                    metadata[OVERALL_FPS]=(reads-1)/(stamp-first_time) if stamp>first_time else 0
                    metadata[DRIVER_FPS]=driver_fps
                    metadata[LAST_READ]=stamp
                    metadata[STATE]=1
                    if unique:
                        sequence+=1
                        target[:frame.size]=frame.reshape(-1)
                        metadata[SEQ]=sequence
                        metadata[STAMP]=metadata[LAST_UNIQUE]=stamp
                        metadata[HEIGHT],metadata[WIDTH]=frame.shape[:2]
                if freshness.stale(stamp,1.0):
                    break
            cap.release()
            cap=None
            if not stop_event.is_set():
                stop_event.wait(0.2)
        with metadata.get_lock():
            metadata[STATE]=7 if stop_event.is_set() else 4
    except Exception as exc:
        print(f"Capture worker error: {type(exc).__name__}: {exc}",flush=True)
        with metadata.get_lock():
            metadata[STATE]=6
    finally:
        if cap is not None:
            cap.release()


class Camera:
    def __init__(self,cfg):
        context=multiprocessing.get_context("spawn")
        self.cfg=cfg
        capacity=max(cfg["width"],1920)*max(cfg["height"],1080)*3
        self.pixels=context.RawArray("B",capacity)
        self.metadata=context.Array("d",14)
        self.stop_event=context.Event()
        self.packet=None
        self.cached_metadata=[0.0]*14
        self.process=context.Process(target=_capture_worker,args=(cfg,self.pixels,self.metadata,self.stop_event),
                                     daemon=True,name="conductor-camera")
        self.process.start()

    def latest(self):
        import numpy as np
        lock=self.metadata.get_lock()
        if not lock.acquire(timeout=0.1):
            return self.packet
        try:
            values=list(self.metadata)
            self.cached_metadata=values
            sequence=int(values[SEQ])
            if sequence and (self.packet is None or sequence!=self.packet[0]):
                height,width=int(values[HEIGHT]),int(values[WIDTH])
                frame=np.frombuffer(self.pixels,dtype=np.uint8,count=height*width*3).reshape(height,width,3).copy()
                self.packet=(sequence,values[STAMP],frame)
            return self.packet
        finally:
            lock.release()

    def stats(self):
        lock=self.metadata.get_lock()
        if lock.acquire(timeout=0.1):
            try:
                self.cached_metadata=list(self.metadata)
            finally:
                lock.release()
        v=self.cached_metadata
        now=time.perf_counter()
        age=now-v[LAST_UNIQUE] if v[LAST_UNIQUE] else None
        state=int(v[STATE])
        status=STATUS.get(state,"UNKNOWN CAMERA STATE")
        if state==1 and age is not None and age>self.cfg["detector"]["max_frame_gap_s"]:
            status="STALLED CAMERA - WAITING FOR FRESH FRAMES"
        mode=(f"{int(v[WIDTH])}x{int(v[HEIGHT])} | driver reports {v[DRIVER_FPS]:.1f} FPS"
              if v[WIDTH] else "pending")
        return {"reads":int(v[READS]),"unique_frames":int(v[SEQ]),
                "exact_duplicate_frames":int(v[DUPLICATES]),
                "capture_fps_recent":round(v[FPS],2) if now-v[LAST_READ]<1.5 else 0.0,
                "capture_fps_overall":round(v[OVERALL_FPS],2),
                "fresh_frame_age_s":round(age,3) if age is not None else None,
                "requested_fourcc":{0:"MJPG",1:"YUY2",2:"NATIVE"}.get(v[FORMAT],"UNKNOWN"),
                "capture_attempt":int(v[ATTEMPT]),"mode":mode,"status":status,
                "worker_pid":self.process.pid,"worker_alive":self.process.is_alive()}

    def close(self):
        self.stop_event.set()
        self.process.join(timeout=3)
        clean=not self.process.is_alive()
        if not clean:
            self.process.terminate()
            self.process.join(timeout=2)
        return clean
