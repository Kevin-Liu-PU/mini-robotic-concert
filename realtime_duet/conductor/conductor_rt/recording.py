"""Opt-in local camera video + monotonic frame index. No audio/keypoints.

A maximum of three raw frames wait for the encoder. Capture never waits for
encoding or disk writes. AVI playback is nominal-CFR; the sidecar records the
actual read-return time and gaps, so dropped frames are never hidden as real time.
"""
import json
from pathlib import Path
import queue
import threading
import time


class CameraRecording:
    def __init__(self, folder, fps=30., queue_size=3):
        self.folder=Path(folder)
        self.path=self.folder/'camera.avi'
        if self.path.exists():raise FileExistsError(self.path)
        self.fps=fps
        self.queue=queue.Queue(maxsize=queue_size)
        self.stopping=threading.Event()
        self.done=threading.Event()
        self.offered=self.enqueued=self.written=self.dropped=0
        self.max_queue_depth=0
        self.error=None
        self.reason=None
        self.first_input_s=self.last_input_s=None
        self.write_ms=[]
        self.final_report=None
        self.thread=threading.Thread(target=self.run,name='camera-video-encoder',daemon=True)
        self.thread.start()

    def offer(self,image,index,read_start_s,input_s):
        if self.stopping.is_set() or self.error:return
        self.offered+=1
        # The capture API returns a fresh ndarray; no other consumer mutates it.
        item=(image,index,read_start_s,input_s,time.perf_counter())
        try:
            self.queue.put_nowait(item);self.enqueued+=1
            self.max_queue_depth=max(self.max_queue_depth,self.queue.qsize())
        except queue.Full:self.dropped+=1

    def stop(self,reason='operator_recording_stop'):
        if not self.stopping.is_set():self.reason=reason
        self.stopping.set()

    def status(self):
        return dict(enabled=True,active=not self.stopping.is_set() and self.error is None,
                    stopping=self.stopping.is_set() and not self.done.is_set(),finalized=self.done.is_set(),
                    offered=self.offered,written=self.written,dropped=self.dropped,
                    queue_depth=self.queue.qsize(),error=self.error,path=str(self.path),reason=self.reason)

    def close(self,reason='session_end',timeout=5.):
        self.stop(reason)
        self.thread.join(timeout)
        return self.done.is_set()

    def run(self):
        try:self.encode()
        except Exception as exc:
            self.error=f'Finalize {type(exc).__name__}: {exc}'
            self.stop('recording_finalize_error')
            self.final_report=dict(self.status(),active=False,finalized=False,error=self.error)
            try:(self.folder/'recording.json').write_text(json.dumps(self.final_report,indent=2))
            except OSError:pass
        finally:self.done.set()

    def encode(self):
        import cv2
        writer=None
        size=None
        previous_index=None
        try:
            with (self.folder/'camera-frames.jsonl').open('x',encoding='utf-8',buffering=1) as index_log:
                while not self.stopping.is_set() or not self.queue.empty():
                    try:image,index,read_start_s,input_s,enqueued_s=self.queue.get(timeout=.1)
                    except queue.Empty:continue
                    actual_size=(int(image.shape[1]),int(image.shape[0]))
                    if writer is None:
                        size=actual_size
                        writer=cv2.VideoWriter(str(self.path),cv2.VideoWriter_fourcc(*'MJPG'),self.fps,size)
                        if not writer.isOpened():raise RuntimeError('MJPG video writer could not open')
                    if actual_size!=size:raise RuntimeError('Camera dimensions changed during recording')
                    begin=time.perf_counter();writer.write(image);end=time.perf_counter()
                    self.write_ms.append((end-begin)*1000)
                    index_log.write(json.dumps(dict(video_frame=self.written,capture_frame=index,
                        capture_read_start_s=read_start_s,input_s=input_s,enqueued_s=enqueued_s,
                        write_start_s=begin,write_end_s=end,
                        skipped_capture_frames=0 if previous_index is None else index-previous_index-1))+'\n')
                    previous_index=index
                    if self.first_input_s is None:self.first_input_s=input_s
                    self.last_input_s=input_s
                    self.written+=1
        except Exception as exc:
            self.error=f'{type(exc).__name__}: {exc}'
            self.stop('recording_error')
        finally:
            if writer is not None:writer.release()
            decode=dict(first_frame=False,last_frame=False,container_frames=0,container_fps=0.)
            if self.path.exists() and self.written:
                cap=cv2.VideoCapture(str(self.path))
                try:
                    decode['container_frames']=int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
                    decode['container_fps']=cap.get(cv2.CAP_PROP_FPS)
                    decode['first_frame']=bool(cap.read()[0])
                    if decode['container_frames']==1:
                        decode['last_frame']=decode['first_frame']
                    else:
                        cap.set(cv2.CAP_PROP_POS_FRAMES,max(0,decode['container_frames']-1))
                        decode['last_frame']=bool(cap.read()[0])
                    # Some OpenCV backends cannot seek very short MJPG AVIs.
                    # Check their actual frames sequentially before failing.
                    if not decode['last_frame']:
                        cap.release();cap=cv2.VideoCapture(str(self.path));decoded=0
                        while cap.read()[0]:decoded+=1
                        decode['sequential_fallback_frames']=decoded
                        decode['last_frame']=decoded==self.written
                finally:cap.release()
                if not (decode['first_frame'] and decode['last_frame'] and decode['container_frames']==self.written):
                    self.error=self.error or 'Saved video did not pass first/last-frame and frame-count verification'
            elif self.error is None:
                self.error='No video frames were written'
            ordered=sorted(self.write_ms)
            self.final_report=dict(self.status(),finalized=True,active=False,stopping=False,
                codec='MJPG',audio=False,source='raw camera, no diagnostic overlay; not ground truth',
                inference_transform='aspect-preserving resize followed by horizontal mirror; stored video is unmirrored',
                clock='same-host time.perf_counter seconds; input_s is read return, NOT sensor exposure',
                nominal_fps=self.fps,playback_duration_s=self.written/self.fps,
                captured_span_s=(self.last_input_s-self.first_input_s) if self.first_input_s is not None else 0.,
                max_queue_depth=self.max_queue_depth,queue_capacity=self.queue.maxsize,
                encoding_ms_p95=ordered[min(len(ordered)-1,int(.95*len(ordered)))] if ordered else None,
                error=self.error,decode=decode)
            (self.folder/'recording.json').write_text(json.dumps(self.final_report,indent=2),encoding='utf-8')
            self.done.set()
