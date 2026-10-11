"""Explicit local-only, silent recording with bounded asynchronous queues.

MJPEG AVI uses checked Python file writes: encoding and disk errors cannot hide
behind VideoWriter.write's void return. Playback FPS is NOT a capture clock.
"""
from collections import deque
import json
import math
import os
from pathlib import Path
from queue import Empty, Full, Queue
import struct
from threading import Event, Lock, Thread
import time


def _chunk(kind, data):
    return kind+struct.pack('<I',len(data))+data+(b'\0' if len(data)%2 else b'')


class MjpegAviWriter:
    """Single video stream, no audio; checked writes and a finalized AVI index."""
    def __init__(self,path,width,height,fps=30):
        self.width,self.height,self.fps=width,height,fps
        self.count=self.max_size=0
        self.index=[]
        self.file=Path(path).open('xb')
        self.closed=False
        try:
            self._write(self._header(0,0))
            self.movi_start=self.file.tell()
            self._write(b'LIST\0\0\0\0movi')
        except Exception:
            self.closed=True;self.file.close()
            raise

    def _write(self,data):
        if self.file.write(data)!=len(data):
            raise OSError('Short video write')

    def _header(self,riff_size,frames):
        avih=struct.pack('<14I',round(1e6/self.fps),self.max_size*self.fps,0,16,
                         frames,0,1,self.max_size,self.width,self.height,0,0,0,0)
        strh=struct.pack('<4s4sIHH8I4h',b'vids',b'MJPG',0,0,0,0,1,self.fps,0,
                         frames,self.max_size,0xffffffff,0,0,0,self.width,self.height)
        strf=struct.pack('<IiiHH4sIiiII',40,self.width,self.height,1,24,b'MJPG',
                         self.width*self.height*3,0,0,0,0)
        header=_chunk(b'avih',avih)+_chunk(b'LIST',b'strl'+_chunk(b'strh',strh)+_chunk(b'strf',strf))
        return b'RIFF'+struct.pack('<I',riff_size)+b'AVI '+_chunk(b'LIST',b'hdrl'+header)

    def write(self,frame):
        import cv2
        if frame.shape!=(self.height,self.width,3) or str(frame.dtype)!='uint8':
            raise ValueError('Recording frame size or format changed')
        ok,jpeg=cv2.imencode('.jpg',frame,[cv2.IMWRITE_JPEG_QUALITY,85])
        if not ok:raise OSError('JPEG encoding failed')
        data=jpeg.tobytes();offset=self.file.tell()-(self.movi_start+8)
        if self.file.tell()+len(data)>3_900_000_000:
            raise OSError('Recording reached the safe AVI size limit')
        self._write(_chunk(b'00dc',data))
        self.index.append(struct.pack('<4sIII',b'00dc',16,offset,len(data)))
        self.count+=1;self.max_size=max(self.max_size,len(data))

    def release(self):
        if self.closed:return
        self.closed=True
        try:
            movie_end=self.file.tell()
            self._write(_chunk(b'idx1',b''.join(self.index)))
            end=self.file.tell()
            self.file.seek(0);self._write(self._header(end-8,self.count))
            self.file.seek(self.movi_start+4);self._write(struct.pack('<I',movie_end-self.movi_start-8))
            self.file.flush();os.fsync(self.file.fileno())
        finally:self.file.close()



class LocalRecording:
    """Bounded video/diagnostic queues and an independent status publisher.

    The app opts into manual_start. Direct synthetic harnesses may start from
    their first submitted frame. Metadata failure never stops the evidence
    writer; a persistent final metadata failure is still a visible ERROR.
    """
    def __init__(self,directory,origin,*,duration=45,fps=30,queue_size=8,
                 scope='source_camera_unmirrored',mirror_display=True,
                 clock=time.perf_counter,writer_factory=MjpegAviWriter,
                 manual_start=False,manifest_replace=None):
        if not math.isfinite(duration) or not 0<duration<=45:
            raise ValueError('Recording duration must be >0 and <=45 seconds')
        if not 1<=queue_size<=16:raise ValueError('Recording queue must be 1..16 frames')
        self.directory=Path(directory);self.directory.mkdir(parents=True,exist_ok=False)
        self.origin=origin;self.duration=duration;self.fps=fps;self.scope=scope
        self.mirror_display=mirror_display;self.clock=clock;self.writer_factory=writer_factory
        self.manual_start=manual_start;self.countdown_deadline=None
        self.frames=Queue(maxsize=queue_size);self.diagnostics=Queue(maxsize=256)
        self.issues=deque(maxlen=256);self.lock=Lock();self.metadata_lock=Lock()
        self.stop_event=Event();self.writer_done=Event();self.finished=Event();self.closing=Event()
        self.started=None;self.last_sequence=None;self.last_stamp=None;self.producer=None
        self.state='READY' if manual_start else 'ARMED';self.reason=None;self.error=None
        self.stream_error=None;self.stop_requested_at=None
        self.submitted=self.written=self.encoded=self.dropped=self.failure_frame_records=0
        self.video_file_created=False
        self.diagnostic_received=self.diagnostic_submitted=self.diagnostic_written=self.diagnostic_drops=0
        self.issue_overflows=0;self.queue_high_water=0;self.max_submit_ms=self.max_encode_ms=0
        self.metadata_warning=None;self.metadata_failures=0;self.metadata_retries=0
        self.metadata_recoveries=0;self.metadata_finalized=False;self.final_status_saved=False
        self.manifest_replace=manifest_replace or (lambda src,dst:src.replace(dst))
        self.worker=Thread(target=self._run,daemon=True,name='conductor-local-video-writer')
        self.publisher=Thread(target=self._publish_loop,daemon=True,name='conductor-status-publisher')
        try:
            self.worker.start();self.publisher.start()
        except Exception as exc:
            self._fail(exc);self.closing.set()
            if self.worker.ident is not None:self.worker.join(timeout=2)
            if self.writer_done.is_set():self._finish_metadata()
            self.finished.set()
            raise

    def request_start(self,source_ready):
        """Only a user key can leave READY; Q can interrupt every later state."""
        with self.lock:
            if self.state!='READY' or not source_ready or self.stop_event.is_set():return False
            self.countdown_deadline=self.clock()+3.0;self.state='COUNTDOWN'
        self._issue('countdown_started',countdown_s=3.0)
        return True

    def _issue(self,reason,**values):
        with self.lock:
            if len(self.issues)==self.issues.maxlen:self.issue_overflows+=1
            self.issues.append(dict(reason=reason,monotonic_s=self.clock(),**values))

    def _fail(self,exc):
        with self.lock:
            message=f'{type(exc).__name__}: {exc}'
            self.stream_error=self.stream_error or message
            self.error=self.error or message;self.state='ERROR';self.reason='recording_error'
        self._issue('recording_error',error=message);self.stop_event.set()
        print('LOCAL RECORDING ERROR: '+message,flush=True)

    def status(self):
        with self.lock:
            done=self.writer_done.is_set()
            return dict(state=self.state,reason=self.reason,error=self.error,
                directory=str(self.directory),scope=self.scope,audio=False,uploaded=False,
                manual_start=self.manual_start,countdown_remaining_s=max(0,self.countdown_deadline-self.clock()) if self.countdown_deadline is not None else None,
                duration_limit_s=self.duration,started_monotonic_s=self.started,
                session_origin_monotonic_s=self.origin,
                remaining_s=max(0,self.duration-(self.clock()-self.started)) if self.started is not None else self.duration,
                playback_fps=self.fps,playback_time_is_capture_time=False,
                mirror_applied_to_video=False,mirror_display=self.mirror_display,
                submitted_frames=self.submitted,written_frames=self.written,encoded_frames=self.encoded,
                video_file_created=self.video_file_created,
                dropped_frames=self.dropped+self.failure_frame_records,
                source_or_queue_drops=self.dropped,failed_frame_records=self.failure_frame_records,
                unwritten_video_frames=max(0,self.submitted-self.encoded) if done else 0,
                unmapped_video_frames=max(0,self.encoded-self.written),
                diagnostic_received=self.diagnostic_received,diagnostic_submitted=self.diagnostic_submitted,
                diagnostic_written=self.diagnostic_written,diagnostic_drops=self.diagnostic_drops,
                pending_diagnostics=0 if done else self.diagnostic_submitted-self.diagnostic_written,
                issue_details_overflow=self.issue_overflows,queue_capacity=self.frames.maxsize,
                queue_high_water=self.queue_high_water,pending_frames=0 if done else self.submitted-self.written,
                max_submit_ms=self.max_submit_ms,max_encode_ms=self.max_encode_ms,
                metadata_warning=self.metadata_warning,metadata_failures=self.metadata_failures,
                metadata_retries=self.metadata_retries,metadata_recoveries=self.metadata_recoveries,
                metadata_finalized=self.metadata_finalized,final_status_saved=self.final_status_saved,
                evidence_finalized=done and self.stream_error is None,
                finalized=self.finished.is_set() and self.error is None and self.metadata_finalized)

    def in_window(self,t):
        with self.lock:return self.started is not None and self.started<=t<self.started+self.duration

    def submit(self,sequence,timestamp,frame):
        before=time.perf_counter()
        if self.stop_event.is_set():return False
        if not math.isfinite(timestamp) or timestamp>self.clock()+.001:
            self._issue('invalid_capture_timestamp',sequence=sequence);return False
        with self.lock:
            if self.manual_start and self.started is None:
                if self.countdown_deadline is None or timestamp<self.countdown_deadline or self.clock()<self.countdown_deadline:return False
            if self.last_sequence is not None and (sequence<=self.last_sequence or timestamp<=self.last_stamp):return False
            if self.started is None:self.started=timestamp;self.state='RECORDING'
            expired=timestamp>=self.started+self.duration or self.clock()>=self.started+self.duration
            missing=max(0,sequence-self.last_sequence-1) if self.last_sequence is not None else 0
            if not expired:self.last_sequence,self.last_stamp=sequence,timestamp
        if expired:self.stop('duration_limit');return False
        if missing:
            with self.lock:self.dropped+=missing
            self._issue('source_sequence_gap',missing_frames=missing,last_sequence=sequence)
        if self.submitted>=math.ceil(self.duration*120):
            self._fail(RuntimeError('Recording source exceeded the 120 FPS resource cap'));return False
        try:
            with self.lock:
                if self.stop_event.is_set():return False
                if self.frames.full():raise Full
                self.frames.put_nowait((sequence,timestamp,frame.copy()));self.submitted+=1
                self.queue_high_water=max(self.queue_high_water,self.frames.qsize())
                self.max_submit_ms=max(self.max_submit_ms,(time.perf_counter()-before)*1000)
            return True
        except Full:
            with self.lock:self.dropped+=1
            self._issue('video_queue_full',sequence=sequence,capture_monotonic_s=timestamp)
            return False

    def add_diagnostic(self,record):
        if not self.in_window(record['capture_monotonic_s']):return False
        reason=None
        with self.lock:
            self.diagnostic_received+=1
            if self.writer_done.is_set():reason='late_diagnostic_after_finalize'
            else:
                try:self.diagnostics.put_nowait(record);self.diagnostic_submitted+=1
                except Full:reason='diagnostic_queue_full'
            if reason:self.diagnostic_drops+=1
        if reason:self._issue(reason,sequence=record['sequence'])
        return reason is None

    def start_source(self,get_packet,max_age=.25):
        def pump():
            last=None
            try:
                while not self.stop_event.is_set():
                    packet=get_packet()
                    if packet and packet[0]!=last:
                        last=packet[0]
                        if 0<=self.clock()-packet[1]<=max_age:self.submit(*packet)
                        else:self._issue('stale_source_packet',sequence=packet[0])
                    self.stop_event.wait(.005)
            except Exception as exc:self._fail(exc)
        self.producer=Thread(target=pump,daemon=True,name='conductor-local-video-source');self.producer.start()

    def stop(self,reason='user_quit'):
        with self.lock:
            if self.reason is None:self.reason=reason
            if self.stop_requested_at is None:self.stop_requested_at=self.clock()
            if self.state not in ('ERROR','CLOSED','CANCELLED'):self.state='FLUSHING'
        self.stop_event.set()

    @staticmethod
    def _line(stream,value):
        value=json.dumps(value,allow_nan=False,separators=(',',':'))+'\n'
        if stream.write(value)!=len(value):raise OSError('Short diagnostic write')

    def _flush_issues(self):
        with self.lock:pending=list(self.issues);self.issues.clear()
        if pending:
            with (self.directory/'recording.issues.jsonl').open('a',encoding='utf-8') as stream:
                for item in pending:self._line(stream,item)
                stream.flush();os.fsync(stream.fileno())

    def _publish_metadata(self,final=False):
        """Bounded retries in the publisher thread, never the video writer."""
        with self.metadata_lock:
            error=None
            for attempt in range(4 if final else 3):
                if attempt:
                    with self.lock:self.metadata_retries+=1
                    time.sleep(.02*(2**(attempt-1)))
                payload=self.status()
                if final:
                    payload.update(state='ERROR' if self.error else 'CLOSED' if self.started is not None else 'CANCELLED',
                                   metadata_finalized=True,finalized=self.error is None)
                try:
                    temporary=self.directory/'manifest.tmp'
                    temporary.write_text(json.dumps(payload,indent=2,allow_nan=False)+'\n',encoding='utf-8')
                    self.manifest_replace(temporary,self.directory/'manifest.json')
                    with self.lock:
                        recovered=self.metadata_warning is not None
                        if recovered:self.metadata_recoveries+=1
                        self.metadata_warning=None
                        if final:self.metadata_finalized=True;self.state=payload['state']
                    if recovered:self._issue('metadata_publication_recovered')
                    return True
                except OSError as exc:
                    error=f'{type(exc).__name__}: {exc}'
                    with self.lock:self.metadata_failures+=1
                    # Sharing violations can resolve shortly; disk/path failures
                    # remain visible but are retried only at the next snapshot.
                    if not isinstance(exc,PermissionError) and getattr(exc,'winerror',None) not in (5,32,33):break
            with self.lock:
                first=self.metadata_warning is None;self.metadata_warning=error
                if final:
                    self.metadata_finalized=False;self.state='ERROR'
                    self.error=self.error or 'Final metadata publication failed: '+str(error)
                    if self.reason is None:self.reason='metadata_finalization_error'
            if first or final:
                self._issue('metadata_publication_failed',final=final,error=error)
                print(('LOCAL RECORDING ERROR: ' if final else 'LOCAL RECORDING STATUS WARNING: ')+str(error),flush=True)
            return False

    def _finish_metadata(self):
        self._publish_metadata(final=True)
        try:self._flush_issues()
        except Exception as exc:
            self._fail(exc)
            self._publish_metadata(final=True)
        # An independent final summary survives a persistently locked manifest.
        # It is never used to pretend a failed final manifest was successful.
        try:
            with (self.directory/'final-status.json').open('w',encoding='utf-8') as stream:
                payload=self.status();payload['finalized']=self.error is None and self.metadata_finalized
                payload['final_status_saved']=True
                self._line(stream,payload);stream.flush();os.fsync(stream.fileno())
            with self.lock:self.final_status_saved=True
        except Exception as exc:
            with self.lock:self.final_status_saved=False
            self._fail(exc)
            self._publish_metadata(final=True)
        # There is no further publication after a successful fallback summary:
        # a new replace failure here would leave that summary falsely successful.
        # A failed final disk write refreshes the manifest as ERROR above. A
        # successful final-status.json is always the last status publication.

    def _publish_loop(self):
        try:
            while not self.writer_done.is_set():
                self._publish_metadata();self.writer_done.wait(.25)
            self._finish_metadata()
        except Exception as exc:self._fail(exc)
        finally:self.finished.set()

    def close(self,reason='user_quit',timeout=8):
        self.stop(reason)
        if self.producer:
            self.producer.join(timeout=1)
            if self.producer.is_alive():self._fail(TimeoutError('Recording source thread did not stop'))
        self.closing.set();self.worker.join(timeout=timeout)
        if self.worker.is_alive():
            self._fail(TimeoutError('Video flush did not finish; file completeness is unconfirmed'));return False
        self.publisher.join(timeout=1)
        if self.publisher.is_alive():
            self._fail(TimeoutError('Final status publication did not finish'));return False
        # Include any diagnostics rejected after automatic writer finalization.
        self._finish_metadata()
        return self.finished.is_set() and self.error is None

    def _run(self):
        video=None;streams=[]
        try:
            mapping=(self.directory/'video.frames.jsonl').open('x',encoding='utf-8');streams.append(mapping)
            diagnostics=(self.directory/'diagnostics.jsonl').open('x',encoding='utf-8');streams.append(diagnostics)
            issues=(self.directory/'recording.issues.jsonl').open('x',encoding='utf-8');streams.append(issues)
            last_flush=0
            while True:
                if self.started is not None and self.clock()>=self.started+self.duration:self.stop('duration_limit')
                try:item=self.frames.get(timeout=.02)
                except Empty:item=None
                if item:
                    sequence,stamp,frame=item
                    if video is None:
                        try:video=self.writer_factory(self.directory/'camera.avi',frame.shape[1],frame.shape[0],self.fps)
                        finally:
                            with self.lock:self.video_file_created=(self.directory/'camera.avi').exists()
                    before=time.perf_counter();video.write(frame)
                    with self.lock:self.encoded+=1
                    elapsed=(time.perf_counter()-before)*1000
                    self._line(mapping,dict(video_frame_index=self.written,sequence=sequence,
                        capture_monotonic_s=stamp,sample_time_s=stamp-self.origin,
                        recording_time_s=stamp-self.started,playback_time_s=self.written/self.fps,
                        encoded_monotonic_s=self.clock()))
                    with self.lock:self.written+=1;self.max_encode_ms=max(self.max_encode_ms,elapsed)
                    self.frames.task_done()
                while True:
                    try:record=self.diagnostics.get_nowait()
                    except Empty:break
                    self._line(diagnostics,record)
                    with self.lock:self.diagnostic_written+=1
                with self.lock:pending=list(self.issues);self.issues.clear()
                for issue in pending:self._line(issues,issue)
                if self.clock()-last_flush>=.25:
                    for stream in streams:stream.flush()
                    last_flush=self.clock()
                settled=self.closing.is_set() or (self.stop_requested_at is not None and self.clock()-self.stop_requested_at>=.35)
                if self.stop_event.is_set() and self.frames.empty() and self.diagnostics.empty() and settled:break
        except Exception as exc:self._fail(exc)
        finally:
            self.stop_event.set()
            if self.producer:
                self.producer.join(timeout=1)
                if self.producer.is_alive():self._fail(TimeoutError('Recording source thread did not stop'))
            try:
                if video:video.release()
            except Exception as exc:self._fail(exc)
            for stream in streams:
                try:stream.flush();os.fsync(stream.fileno())
                except Exception as exc:self._fail(exc)
                finally:
                    try:stream.close()
                    except Exception as exc:self._fail(exc)
            with self.lock:
                self.failure_frame_records=self.submitted-self.written
                self.diagnostic_drops+=self.diagnostic_submitted-self.diagnostic_written
                while not self.frames.empty():self.frames.get_nowait()
                while not self.diagnostics.empty():self.diagnostics.get_nowait()
                self.writer_done.set()
