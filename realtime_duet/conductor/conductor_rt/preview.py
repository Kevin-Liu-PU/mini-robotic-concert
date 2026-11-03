"""RAM-only latest preview over an inherited anonymous pipe; no image files.

Encoding is off the vision/clock threads. A size-one queue drops old preview
frames; this never drives musical decisions. Only the owning launcher receives it.
"""
import os
import queue
import struct
import threading
import time


class PreviewWriter:
    def __init__(self, fd):
        self.fd=fd
        self.queue=queue.Queue(maxsize=1)
        self.closed=threading.Event()
        self.thread=threading.Thread(target=self.run,name='ram-preview-writer',daemon=True)
        self.thread.start()

    def offer(self,image,record,selected_side,recording=False):
        item=(image,record,selected_side,recording)
        try:self.queue.put_nowait(item)
        except queue.Full:
            try:self.queue.get_nowait()
            except queue.Empty:pass
            try:self.queue.put_nowait(item)
            except queue.Full:pass

    def run(self):
        import cv2
        try:
            while not self.closed.is_set():
                try:image,record,side,recording=self.queue.get(timeout=.2)
                except queue.Empty:continue
                image=image.copy();h,w=image.shape[:2]
                pose=record.get('pose')
                if pose:
                    for i,j in ((11,12),(11,13),(13,15),(12,14),(14,16)):
                        if min(pose[i][2],pose[j][2])>=.5:
                            cv2.line(image,(round(pose[i][0]*w),round(pose[i][1]*h)),
                                (round(pose[j][0]*w),round(pose[j][1]*h)),(0,220,255),2)
                    # Holistic sees reflected pixels; label the person's
                    # physical hand, not the model's reflected-side enum.
                    for i,label in ((15,'Your RIGHT'),(16,'Your LEFT')):
                        x,y=round(pose[i][0]*w),round(pose[i][1]*h)
                        color=(0,255,0) if side==i-15 else (0,150,255)
                        cv2.circle(image,(x,y),7,color,2)
                        cv2.putText(image,f'{label} {pose[i][2]:.2f}',(max(0,min(w-80,x)),max(15,min(h-10,y-12))),
                                    cv2.FONT_HERSHEY_SIMPLEX,.45,color,1)
                face=record.get('face')
                if face:
                    p=face['152'];cv2.circle(image,(round(p[0]*w),round(p[1]*h)),5,(255,180,0),2)
                cv2.putText(image,'REC: raw camera saved locally' if recording else 'LIVE PREVIEW - recording OFF',(8,h-12),cv2.FONT_HERSHEY_SIMPLEX,.45,(0,255,0),1)
                ok,encoded=cv2.imencode('.jpg',image,[cv2.IMWRITE_JPEG_QUALITY,75])
                if ok:
                    data=struct.pack('!I',len(encoded))+encoded.tobytes()
                    while data:
                        written=os.write(self.fd,data)
                        data=data[written:]
                self.closed.wait(.08)
        except (OSError,ValueError):pass
        finally:
            try:os.close(self.fd)
            except OSError:pass

    def close(self):
        self.closed.set()
        self.thread.join(timeout=.3)


class PreviewReader:
    def __init__(self,fd):
        self.fd=fd
        self.image=None
        self.sequence=0
        self.received_at=None
        self.lock=threading.Lock()
        self.thread=threading.Thread(target=self.run,name='ram-preview-reader',daemon=True)
        self.thread.start()

    def read_exact(self,n):
        data=bytearray()
        while len(data)<n:
            chunk=os.read(self.fd,n-len(data))
            if not chunk:return None
            data.extend(chunk)
        return bytes(data)

    def run(self):
        try:
            while True:
                header=self.read_exact(4)
                if header is None:break
                size=struct.unpack('!I',header)[0]
                if not 0<size<2_000_000:break
                image=self.read_exact(size)
                if image is None:break
                with self.lock:
                    self.image=image;self.sequence+=1;self.received_at=time.perf_counter()
        except OSError:pass
        finally:
            try:os.close(self.fd)
            except OSError:pass

    def latest(self):
        with self.lock:return self.image,self.sequence,self.received_at
