"""PTS-paced video runner; camera source is explicit opt-in and unvalidated.

Video tests never open a camera. Optional beat audio is output-only; no robot APIs.
"""
import argparse
from collections import deque
from dataclasses import asdict
import json
import math
import os
from pathlib import Path
import queue
import statistics
import threading
import time
import traceback
import hashlib
import sys
from . import __version__
from .clock import MusicalClock
from .count_in import CountIn
from .body_reference import AutomaticVisualDetector, tracking_timeout, pose_side_for_hand
from .protocol import WireEncoder
from .transport import Publisher
from .vision import VisualDetector, result_record
from .timing import precise_wait
from .camera_setup import camera_backend,configure_camera,prepare_frame,load_calibration,validate_calibration


def frame_log_record(metric, detection, landmarks, camera=False):
    if camera:
        # Camera trial keeps performance/events, not pixels, landmark vectors,
        # hand-shape measurements or body/fingertip positions.
        return dict(metric, detection={key:detection[key] for key in ('prepare','stop','visible','association','reference_state','reference_reason','reference_quality','reference_frame_gap_s','reference_gap_resumed','stop_checks','stop_approach_age_s','stop_confirmation_s','tracking_ready','wrist_visibility','detected_hands','selected_side','locked_side','hand_mesh_age_ms','beat_stage','beat_rejection','candidate_kind','candidate_stamp','candidate_frame','candidate_feature','candidate_confirmation_ms','downstroke_shoulder_units','rebound_shoulder_units') if key in detection},
                    camera_landmarks_saved=False)
    return dict(metric, detection=detection, landmarks=landmarks)


def distribution(values):
    values = sorted(values)
    if not values:
        return None
    def percentile(q):
        index = (len(values)-1)*q
        a = int(index)
        b = min(len(values)-1, a+1)
        return values[a]+(values[b]-values[a])*(index-a)
    return dict(n=len(values), p50=percentile(.5), p95=percentile(.95), p99=percentile(.99), max=values[-1])


class LatestFrame:
    def __init__(self):
        self.lock = threading.Lock()
        self.frame = None
        self.dropped = 0
        self.max_depth = 0

    def put(self, frame):
        with self.lock:
            if self.frame is not None:
                self.dropped += 1
            self.frame = frame
            self.max_depth = 1

    def take(self):
        with self.lock:
            item, self.frame = self.frame, None
        return item


class VideoRun:
    def __init__(self, args):
        self.args = args
        self.frames = LatestFrame()
        self.commands = queue.Queue(maxsize=64)
        self.stop_request = threading.Event()
        self.stop_info = None
        self.reader_done = threading.Event()
        self.vision_done = threading.Event()
        self.cancel = threading.Event()
        self.epoch = None
        self.decoded = self.processed = self.command_drops = 0
        self.latest_visible = None
        self.latest_detection = {}
        self.visual_candidates = []
        self.reference_counts = {}
        self.errors = []
        self.frame_metrics = []
        self.control_records = []
        self.events = []
        self.clock_intervals_ms = []
        self.controller_ms = []
        self.model_cold_s = None
        self.metadata = {}
        self.recorder = None
        self.audio = None
        self.recording_stop = threading.Event()
        self.code_hashes = {p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in Path(__file__).parent.glob("*.py")}

    def request_stop(self, reason, **extra):
        self.stop_info = dict(reason=reason, **extra)
        self.stop_request.set()

    def submit(self, kind, payload):
        try:
            self.commands.put_nowait((kind, payload))
        except queue.Full:
            self.command_drops += 1

    def read_operator_controls(self):
        """Dedicated pipe reader; no stdin I/O on the musical clock thread."""
        for line in sys.stdin:
            command=line.strip().lower()
            if command=="record-stop":
                self.control_records.append(dict(action="recording_stop",reason="operator_recording_stop",applied_s=time.perf_counter()))
                self.recording_stop.set()
                if self.recorder is not None:self.recorder.stop()
                continue
            if command in ("stop","quit"):
                self.request_stop("operator_pipe_stop")
                if command=="quit":
                    self.cancel.set()
            elif command in ("prepare","start"):
                self.submit("operator",command)
            if self.cancel.is_set():
                break

    def read_video(self):
        import cv2
        cap = None
        try:
            self.epoch = time.perf_counter()+.1
            offset = 0.
            for repeat in range(self.args.repeat):
                cap = cv2.VideoCapture(str(self.args.video))
                if not cap.isOpened():
                    raise RuntimeError("Video file could not be opened")
                fps = cap.get(cv2.CAP_PROP_FPS)
                self.metadata.update(width=int(cap.get(cv2.CAP_PROP_FRAME_WIDTH)), height=int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT)), nominal_fps=fps)
                index = 0
                last_pts = -1.
                while not self.cancel.is_set():
                    read_begin = time.perf_counter()
                    ok, image = cap.read()
                    read_end = time.perf_counter()
                    if not ok:
                        break
                    pts = cap.get(cv2.CAP_PROP_POS_MSEC)/1000
                    if not math.isfinite(pts) or pts < 0 or index > 0 and pts <= last_pts:
                        raise RuntimeError("Non-monotonic/missing video PTS; do not fake timestamps with nominal FPS")
                    last_pts = pts
                    due = self.epoch + offset + pts
                    if self.args.duration and offset+pts >= self.args.duration:
                        self.cancel.set()
                        break
                    remaining = due-time.perf_counter()
                    if remaining > 0:
                        precise_wait(remaining)
                    if self.cancel.is_set():
                        break
                    arrived = time.perf_counter()
                    self.frames.put(dict(image=image, frame=self.decoded, clip_frame=index, repeat=repeat,
                                         source_s=offset+pts, pts_s=pts, input_s=arrived, scheduled_s=due,
                                         decode_ms=(read_end-read_begin)*1000))
                    self.decoded += 1
                    index += 1
                cap.release()
                cap = None
                offset += last_pts+1/fps
                if self.cancel.is_set():
                    break
        except Exception:
            self.errors.append(traceback.format_exc())
            self.request_stop("source_fault")
        finally:
            if cap is not None:
                cap.release()
            self.reader_done.set()

    def read_camera(self):
        # This branch is never entered by file replays or the synthetic suite.
        # Camera trial requires separate user approval plus explicit CLI enable.
        import cv2
        cap=None
        try:
            if not self.args.enable_camera or self.args.camera_index is None:
                raise RuntimeError("Camera source is not explicitly enabled")
            backend, backend_name = camera_backend(cv2)
            calibration=getattr(self.args,'calibration_data',None)
            if calibration is not None:
                validate_calibration(calibration,self.args.camera_index,self.args.width,backend)
            cap=cv2.VideoCapture(self.args.camera_index,backend)
            if not cap.isOpened():
                raise RuntimeError("Selected camera could not be opened")
            buffer_request=configure_camera(cv2,cap,self.args.width)
            self.metadata.update(width=int(cap.get(cv2.CAP_PROP_FRAME_WIDTH)),height=int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT)),
                                 nominal_fps=cap.get(cv2.CAP_PROP_FPS),camera_buffer_request_accepted=buffer_request,
                                 sensor_exposure_timestamp_available=False,camera_path_unvalidated=True,
                                 camera_backend_requested=backend_name,camera_landmarks_saved=False,
                                 video_recording=bool(getattr(self.args,"record_video",False)),audio_playback=False)
            self.epoch=time.perf_counter()
            if getattr(self.args,'record_video',False) and not self.recording_stop.is_set():
                from .recording import CameraRecording
                self.recorder=CameraRecording(self.args.output,fps=30.)
            while not self.cancel.is_set():
                begin=time.perf_counter()
                ok,image=cap.read()
                arrived=time.perf_counter()
                if not ok:
                    raise RuntimeError("Camera read failed")
                if calibration is not None:
                    validate_calibration(calibration,self.args.camera_index,self.args.width,backend,
                                         (int(image.shape[1]),int(image.shape[0])))
                self.frames.put(dict(image=image,frame=self.decoded,clip_frame=self.decoded,repeat=0,
                                     source_s=arrived-self.epoch,pts_s=None,input_s=arrived,scheduled_s=arrived,
                                     decode_ms=(arrived-begin)*1000))
                if self.recorder is not None and not self.recording_stop.is_set():
                    self.recorder.offer(image,self.decoded,begin,arrived)
                self.decoded+=1
                if self.args.duration and arrived-self.epoch >= self.args.duration:
                    self.request_stop("diagnostic_duration_limit")
                    self.cancel.set()
        except Exception:
            self.errors.append(traceback.format_exc())
            self.request_stop("camera_fault")
        finally:
            if cap is not None:
                cap.release()
            if self.recorder is not None:self.recorder.close("camera_session_end")
            self.reader_done.set()

    def infer(self, model):
        import cv2
        detector = None
        preview=None
        if self.args.preview_fd is not None:
            from .preview import PreviewWriter
            preview=PreviewWriter(self.args.preview_fd)
        try:
            last_prepare_status=None
            with (self.args.output / "frames.jsonl").open("w", encoding="utf-8") as log, \
                 (self.args.output / "gesture-diagnostics.jsonl").open("w",encoding="utf-8",buffering=1) as gesture_log:
                while True:
                    packet = self.frames.take()
                    if packet is None:
                        if self.reader_done.is_set():
                            break
                        precise_wait(.001)
                        continue
                    begin = time.perf_counter()
                    image = packet.pop("image")
                    if detector is None:
                        detector = (AutomaticVisualDetector(image.shape[1]/image.shape[0],
                            conducting_side=pose_side_for_hand(self.args.conducting_hand),meter=self.args.meter) if self.args.chin_y is None else
                                    VisualDetector(self.args.chin_y, image.shape[1]/image.shape[0], self.args.geometry))
                        if not isinstance(detector,AutomaticVisualDetector):
                            detector.side=pose_side_for_hand(self.args.conducting_hand)
                    # One decode and one downscale. Mirror the smaller image.
                    small = prepare_frame(cv2,image,self.args.width)
                    rgb = cv2.cvtColor(small, cv2.COLOR_BGR2RGB)
                    model_begin = time.perf_counter()
                    result = model.process(rgb)
                    model_end = time.perf_counter()
                    raw = result_record(result)
                    if isinstance(detector,AutomaticVisualDetector):detector.control_state=self.current_state
                    detection = detector.update(raw, packet["input_s"], model_end, packet["source_s"], packet["frame"], self.current_bpm)
                    detector_end = time.perf_counter()
                    if preview is not None:preview.offer(small,raw,detection.get("selected_side"),
                        self.recorder is not None and self.recorder.status()["active"])
                    self.latest_detection = {k:detection[k] for k in ('visible','association','reference_state',
                        'reference_reason','reference_quality','reference_frame_gap_s','reference_gap_resumed','stop_checks','stop_approach_age_s','stop_confirmation_s','tracking_ready','wrist_visibility','detected_hands','selected_side','locked_side','hand_mesh_age_ms','beat_stage','beat_rejection','downstroke_shoulder_units','rebound_shoulder_units','prepare_hands') if k in detection}
                    prepare_status=detection.get('prepare_hands')
                    if prepare_status != last_prepare_status:
                        gesture_log.write(json.dumps(dict(frame=packet['frame'],input_s=packet['input_s'],
                            decision_s=detector_end,prepare_hands=prepare_status,selected_side=detection.get('selected_side')))+'\n')
                        last_prepare_status=prepare_status
                    reference_state = detection.get('reference_state', 'manual')
                    self.reference_counts[reference_state] = self.reference_counts.get(reference_state, 0)+1
                    if detection['observation'] or detection['prepare'] or detection['stop']:
                        obs = detection['observation']
                        self.visual_candidates.append(dict(frame=packet['frame'], input_s=packet['input_s'],
                            decision_s=detector_end, prepare=detection['prepare'], prepare_hand=detection.get('prepare_hand'), stop=detection['stop'],
                            observation=asdict(obs) if obs else None, reference_state=reference_state,
                            candidate_feature=detection.get('candidate_feature')))
                    if detection.get('tracking_ready', detection["visible"]):
                        self.latest_visible = packet["input_s"]
                    if self.args.experimental_auto_stop and detection["stop"] and self.current_state == "running":
                        self.request_stop("experimental_confirmed_pinch", frame=packet["frame"], input_s=packet["input_s"], source_s=packet["source_s"], evidence_s=detection.get("stop_evidence_stamp"), confirmation_s=detection.get("stop_confirmation_s"))
                    if self.args.auto_gestures and detection["prepare"]:
                        self.submit("prepare", dict(frame=packet["frame"], input_s=packet["input_s"], source_s=packet["source_s"]))
                    if detection["observation"]:
                        self.submit("observation", detection["observation"])
                    metric = dict(packet, model_start_s=model_begin, model_end_s=model_end, detector_end_s=detector_end,
                                  queue_ms=(begin-packet["input_s"])*1000,
                                  schedule_late_ms=(packet["input_s"]-packet["scheduled_s"])*1000,
                                  model_ms=(model_end-model_begin)*1000,
                                  input_to_detection_ms=(detector_end-packet["input_s"])*1000,
                                  preprocess_ms=(model_begin-begin)*1000,
                                  detector_ms=(detector_end-model_end)*1000)
                    self.frame_metrics.append(metric)
                    log_detection = dict(detection)
                    if log_detection["observation"]:
                        log_detection["observation"] = asdict(log_detection["observation"])
                    log.write(json.dumps(frame_log_record(metric,log_detection,raw,
                        camera=self.args.camera_index is not None or self.args.private_input), allow_nan=False)+"\n")
                    self.processed += 1
        except Exception:
            self.errors.append(traceback.format_exc())
            self.request_stop("vision_fault")
        finally:
            if preview is not None:preview.close()
            model.close()
            self.vision_done.set()

    def run(self):
        process_started = time.perf_counter()
        os.environ.setdefault("OMP_NUM_THREADS", "4")
        os.environ.setdefault("OPENBLAS_NUM_THREADS", "1")
        import cv2
        import mediapipe as mp
        cv2.setNumThreads(1)
        model = mp.solutions.holistic.Holistic(static_image_mode=False, model_complexity=1,
                                             smooth_landmarks=True, refine_face_landmarks=False,
                                             min_detection_confidence=.6, min_tracking_confidence=.6)
        self.model_cold_s = time.perf_counter()-process_started
        self.clock = MusicalClock(self.args.initial_bpm, self.args.meter, self.args.session_id,
                                  score_beats=None if self.args.continuous_test else 12*self.args.meter)
        self.current_bpm = self.clock.bpm
        self.current_state = self.clock.state
        encoder = WireEncoder(self.args.clock_domain)
        if self.args.beat_audio:
            from .beat_audio import BeatAudio
            self.audio = BeatAudio(self.args.output)
            try:self.audio.start()
            except Exception:
                model.close()
                raise
        publisher = Publisher(self.args.output, self.args.tcp_port if self.args.transport == "tcp" else None, self.audio)
        publisher.thread.start()
        publisher.ready.wait(5)
        if publisher.error:
            if self.audio is not None:self.audio.close()
            model.close()
            raise RuntimeError(publisher.error)
        if self.args.wait_client:
            print("Waiting for loopback consumer; hardware remains outside this program", flush=True)
            connect_deadline=time.perf_counter()+self.args.wait_client
            while not publisher.connected.is_set() and not publisher.error and time.perf_counter()<connect_deadline:
                precise_wait(.01)
            if not publisher.connected.is_set():
                publisher.close()
                if self.audio is not None:self.audio.close()
                model.close()
                raise RuntimeError(publisher.error or "No consumer within --wait-client deadline")
        reader = threading.Thread(target=self.read_video if self.args.video else self.read_camera, name="frame-input", daemon=True)
        vision = threading.Thread(target=self.infer, args=(model,), name="holistic-worker", daemon=True)
        reader.start()
        vision.start()
        if self.args.stdin_controls:
            threading.Thread(target=self.read_operator_controls,name="operator-controls",daemon=True).start()
        next_snapshot = 0.
        count_in = CountIn(self.args.meter)
        next_status = 0.
        last_tick = None
        stop_applied = False
        start_applied = False
        timed_stop_applied = False
        source_finished_at = None
        reason = None
        cause = {}
        try:
            while True:
                now = time.perf_counter()
                if last_tick is not None:
                    self.clock_intervals_ms.append((now-last_tick)*1000)
                last_tick = now
                tick_begin = now
                self.clock.last_visual = self.latest_visible
                outgoing = []
                if publisher.error or publisher.disconnected.is_set() and not publisher.acknowledged.is_set():
                    if not stop_applied:
                        self.request_stop("publisher_fault" if publisher.error else "receiver_disconnected")
                    if self.args.camera_index is not None:
                        self.cancel.set()
                if self.args.stop_at is not None and not timed_stop_applied and self.epoch is not None and now >= self.epoch+self.args.stop_at:
                    self.request_stop("explicit_test_stop")
                    timed_stop_applied = True
                if self.args.camera_index is not None and tracking_timeout(now, self.clock.state,
                        self.clock.armed_at, self.latest_visible, self.args.tracking_timeout):
                    if self.args.transport=='file':
                        outgoing += self.clock.pause_tracking(now)
                        count_in.reset()
                        self.control_records.append(dict(action='tracking_pause',reason='tracking_lost_pause',
                            applied_s=now,latest_reliable_s=self.latest_visible,reference=self.latest_detection))
                    else:
                        self.request_stop('tracking_lost_safety_timeout')
                if now >= next_status:
                    last_metric = self.frame_metrics[-1] if self.frame_metrics else {}
                    recent = self.frame_metrics[-60:]
                    fps = ((len(recent)-1)/(recent[-1]['model_end_s']-recent[0]['model_end_s'])
                           if len(recent)>1 else None)
                    status = dict(state=self.clock.state, bpm=self.clock.bpm, phase=self.clock.phase,
                        meter=self.clock.meter,conducting_hand=self.args.conducting_hand,
                        elapsed_s=now-self.epoch if self.epoch is not None else 0.,
                        session_limit_s=self.args.duration,camera_active=self.epoch is not None and not self.reader_done.is_set(),
                        camera_frames=self.decoded, inferred_frames=self.processed,
                        dropped_frames=self.frames.dropped, recent_fps=fps, stop_reason=self.clock.stopped_reason,
                        model_ms=last_metric.get('model_ms'), input_to_detection_ms=last_metric.get('input_to_detection_ms'),
                        recording=self.recorder.status() if self.recorder is not None else dict(enabled=False,active=False),
                        audio=self.audio.status() if self.audio is not None else dict(enabled=False,active=False),
                        tracking=self.latest_detection, visual_candidates=len(self.visual_candidates),
                        count_in_cues=len(count_in.cues), count_in_failed=count_in.failed,
                        count_in_reason=count_in.last_reason,count_in_resets=count_in.resets,
                        latest_visual_candidate=(self.visual_candidates[-1]["observation"]["kind"] if self.visual_candidates and self.visual_candidates[-1]["observation"] else None),
                        accepted=self.clock.accepted, rejected=self.clock.rejected, monotonic_s=now)
                    status_path=self.args.output/'live-status.json'
                    temp_status=self.args.output/'live-status.tmp'
                    temp_status.write_text(json.dumps(status,allow_nan=False),encoding='utf-8')
                    temp_status.replace(status_path)
                    next_status=now+.5
                # STOP precedes all pending observations and predicted beats.
                if self.stop_request.is_set() and not stop_applied:
                    reason = self.stop_info or {"reason":"requested"}
                    outgoing += self.clock.stop(now, reason["reason"])
                    self.control_records.append(dict(reason, action="stop", applied_s=now))
                    if reason.get("input_s") is not None:
                        cause=dict(cause_frame=reason["frame"],decision_monotonic_ns=round(reason["input_s"]*1e9),evidence_monotonic_ns=round((reason.get("evidence_s") or reason["input_s"])*1e9))
                    stop_applied = True
                if self.args.start_at is not None and not start_applied and self.epoch is not None and now >= self.epoch+self.args.start_at:
                    outgoing += self.clock.arm(now, "explicit_test_start")
                    outgoing += self.clock.start(now, "explicit_test_start")
                    start_applied = True
                for _ in range(8):
                    try:
                        kind, payload = self.commands.get_nowait()
                    except queue.Empty:
                        break
                    if kind == "prepare":
                        outgoing += self.clock.arm(now, "visual_L")
                        self.control_records.append(dict(payload, action="prepare", applied_s=now))
                    elif kind == "operator":
                        if payload=="prepare":
                            outgoing+=self.clock.arm(now,"keyboard_prepare")
                        elif payload=="start":
                            if self.args.startup=='explicit':
                                outgoing+=self.clock.start(now,"keyboard_start")
                            else:
                                self.control_records.append(dict(action='start_rejected',rejection='count_in_requires_complete_visual_bar',applied_s=now))
                    elif kind == "observation":
                        if payload.decision_stamp is not None:
                            cause=dict(cause_frame=payload.decision_frame, decision_monotonic_ns=round(payload.decision_stamp*1e9), evidence_monotonic_ns=round(payload.stamp*1e9))
                        if self.clock.state == 'armed' and self.args.startup == 'count-in':
                            count_record,estimated=count_in.observe(payload,now,self.clock.armed_at)
                            self.control_records.append(count_record)
                            if estimated is not None:
                                self.clock.initial_bpm=estimated
                                started=self.clock.start(now,'causal_count_in_downbeat',payload)
                                outgoing+=started
                                self.control_records.append(dict(asdict(payload),action='start' if started else 'start_rejected',
                                    rejection=self.clock.last_start_rejection,applied_s=now,armed_at=self.clock.armed_at,
                                    initial_tempo_source='completed_causal_count_in',initial_bpm=estimated))
                        elif self.clock.state == "armed" and payload.kind == "wrist_rebound":
                            started=self.clock.start(now, "visual_first_downbeat", payload)
                            outgoing += started
                            self.control_records.append(dict(asdict(payload), action="start" if started else "start_rejected",
                                                             rejection=self.clock.last_start_rejection,applied_s=now,armed_at=self.clock.armed_at))
                        else:
                            admitted = self.clock.observe(payload, now)
                            self.control_records.append(dict(admitted, action="observation"))
                if self.args.keyboard:
                    import msvcrt
                    while msvcrt.kbhit():
                        key = msvcrt.getwch()
                        if key in (" ", "\x1b", "q"):
                            self.request_stop("keyboard_stop")
                            outgoing += self.clock.stop(now, "keyboard_stop")
                            if key in ("\x1b","q"):
                                self.cancel.set()
                        elif key == "\r":
                            outgoing += self.clock.arm(now, "keyboard_prepare")
                        elif key.lower() == "s":
                            if self.args.startup=='explicit':
                                outgoing += self.clock.start(now, "keyboard_start")
                outgoing += self.clock.tick(now)
                self.current_bpm = self.clock.bpm
                self.current_state = self.clock.state
                self.events.extend(outgoing)
                if outgoing or now >= next_snapshot:
                    message = encoder.encode(self.clock.message(now, reason=self.clock.stopped_reason))
                    message.update(cause)
                    publisher.offer(message, outgoing)
                    next_snapshot = now+.02
                self.controller_ms.append((time.perf_counter()-tick_begin)*1000)
                if self.args.camera_index is not None and self.clock.state in ("stopped","ended") and (publisher.acknowledged.is_set() or self.args.transport=="file"):
                    self.cancel.set()
                if self.vision_done.is_set():
                    if source_finished_at is None:
                        source_finished_at = now
                        self.request_stop("source_end")
                    # TCP live mode repeats STOP until ACK, or explicit test
                    # deadline when --ack-wait is provided. Default is unbounded.
                    if stop_applied and (self.args.transport == "file" or publisher.acknowledged.is_set() or self.args.ack_wait is not None and now-source_finished_at >= self.args.ack_wait):
                        if now-source_finished_at >= .1:
                            break
                precise_wait(.002)
        except KeyboardInterrupt:
            now = time.perf_counter()
            interrupted = self.clock.stop(now, "operator_interrupt")
            self.events.extend(interrupted)
            publisher.offer(encoder.encode(self.clock.message(now, reason="operator_interrupt")), interrupted)
            self.cancel.set()
        finally:
            self.cancel.set()
            reader.join(3)
            vision.join(5)
            if self.recorder is not None:self.recorder.close("session_end",timeout=5.)
            publisher.close()
            if self.audio is not None:self.audio.close()
        end = time.perf_counter()
        for name, rows in (("events.jsonl", self.events), ("observations.jsonl", self.control_records),
                           ("visual-candidates.jsonl", self.visual_candidates)):
            with (self.args.output/name).open("w", encoding="utf-8") as file:
                for row in rows:
                    file.write(json.dumps(row, allow_nan=False)+"\n")
        metrics = {key:distribution([r[key] for r in self.frame_metrics]) for key in
                   ("decode_ms", "queue_ms", "schedule_late_ms", "preprocess_ms", "model_ms", "detector_ms", "input_to_detection_ms")}
        latency = [(r["processed"]-r["stamp"])*1000 for r in self.control_records if r.get("action") == "observation"]
        metrics.update(observation_to_controller_ms=distribution(latency), controller_tick_ms=distribution(self.controller_ms),
                       clock_wakeup_interval_ms=distribution(self.clock_intervals_ms),
                       snapshot_to_publication_ms=distribution(publisher.publish_delays_ms),
                       snapshot_to_socket_ms=distribution(publisher.socket_delays_ms))
        metrics.update(decision_frame_to_publication_ms=distribution(publisher.decision_to_publication_ms),
                       evidence_frame_to_publication_ms=distribution(publisher.evidence_to_publication_ms))
        duration = end-self.epoch if self.epoch else 0
        metrics.update(version=1, input=str(self.args.video), session_id=self.clock.session_id,
                       clock_domain=self.args.clock_domain, configuration=vars(self.args),
                       cold_import_and_model_init_s=self.model_cold_s, warm_run_wall_s=duration,
                       frames_decoded=self.decoded, frames_inferred=self.processed,
                       processed_fps=self.processed/duration if duration else None,
                       dropped_frames=self.frames.dropped, dropped_fraction=self.frames.dropped/max(1,self.decoded),
                       longest_frame_queue=self.frames.max_depth, command_drops=self.command_drops,
                       transport_coalesced=publisher.coalesced, published_snapshots=publisher.sent,
                       published_events=publisher.published_events,event_publication_drops=publisher.event_drops,
                       audio=self.audio.status() if self.audio is not None else dict(enabled=False,active=False),
                       software_stop_ack=publisher.stop_ack, stop_ack_is_physical_stop=False,
                       skipped_clock_deadlines=self.clock.skipped_deadlines,
                       final_bpm=self.clock.bpm, final_phase=self.clock.phase, final_state=self.clock.state,
                       stop_reason=self.clock.stopped_reason,
                       recording=(self.recorder.final_report or self.recorder.status()) if self.recorder is not None else dict(enabled=False),
                       reference_state_frames=self.reference_counts,
                       perception_mode="automatic_body_relative" if self.args.chin_y is None else "manual_image_line",
                       metadata=self.metadata, epoch_monotonic_s=self.epoch, errors=self.errors,
                       publisher_error=publisher.error, hardware_calls=0, camera_calls=int(self.args.camera_index is not None))
        metrics.update(implementation_version=__version__,source_hashes_at_start=self.code_hashes)
        metrics['metadata']['audio_playback']=self.args.beat_audio
        metrics["connect_errors"] = publisher.connect_errors
        metrics["configuration"] = {k:str(v) if isinstance(v,Path) else v for k,v in vars(self.args).items()}
        (self.args.output/"metrics.json").write_text(json.dumps(metrics,indent=2,allow_nan=False),encoding="utf-8")
        summary = {k:metrics[k] for k in ("frames_decoded","frames_inferred","dropped_frames","processed_fps","model_ms","input_to_detection_ms","clock_wakeup_interval_ms","final_state","final_bpm","errors")}
        print(json.dumps(summary,indent=2),flush=True)
        return metrics


def main():
    p = argparse.ArgumentParser(description=__doc__)
    source=p.add_mutually_exclusive_group(required=True)
    source.add_argument("--video", type=Path, help="Existing video file; never interpreted as a camera number")
    source.add_argument("--camera-index",type=int,help="Unvalidated future camera trial; requires --enable-camera and separate user approval")
    p.add_argument("--enable-camera",action="store_true",help="Explicit opt-in only after camera trial authorization; never used by video tests")
    p.add_argument("--initial-bpm", type=float, required=True)
    p.add_argument('--startup',choices=('explicit','count-in'),default='explicit',help='count-in consumes a full known 4/4 bar before score start; initial BPM is only a detector hint until then')
    calibration_group=p.add_mutually_exclusive_group()
    calibration_group.add_argument("--chin-y", type=float, help="Debug fallback: fixed normalized image chin calibration; default auto body-relative reference")
    calibration_group.add_argument('--calibration',type=Path,help='Numeric camera calibration JSON; camera mode only')
    p.add_argument("--output", type=Path, required=True, help="New isolated run directory; must not exist")
    p.add_argument("--clock-domain", required=True)
    p.add_argument("--session-id")
    p.add_argument("--meter", type=int, choices=(3,4), default=4)
    p.add_argument('--conducting-hand',choices=('left','right'),default='right',help='Fixed anatomical hand for L, beats and Stop; never auto-switches')
    p.add_argument('--private-input',action='store_true',help='Keep numeric/event diagnostics only for an authorized local recording; never log landmarks')
    p.add_argument("--width", type=int, default=640)
    p.add_argument("--geometry", choices=("native","legacy"), default="native")
    p.add_argument("--transport", choices=("tcp","file"), default="tcp")
    p.add_argument("--tcp-port", type=int, default=8766, help="Connect to mock receiver listening on 127.0.0.1")
    p.add_argument("--wait-client", type=float, default=0)
    p.add_argument("--ack-wait", type=float, help="Explicit replay teardown limit; missing ACK is reported, never assumed")
    p.add_argument("--start-at", type=float, help="Explicit test/manual start time in source seconds; not a visual success")
    p.add_argument("--stop-at", type=float, help="Explicit scheduled operator-Stop test; never counted as recognized gesture")
    p.add_argument("--no-auto-gestures", dest="auto_gestures", action="store_false")
    p.add_argument("--experimental-auto-stop", action="store_true", help="Known false-positive pinch detector; default OFF. Use explicit keyboard Stop for the testable path.")
    p.add_argument("--keyboard", action="store_true", help="Enter=prepare, S=start with configured BPM, Space=Stop, console focus required")
    p.add_argument("--stdin-controls",action="store_true",help="Launcher-only line protocol: prepare/start/stop/quit; reader is off the clock thread")
    p.add_argument("--repeat", type=int, default=1)
    p.add_argument("--record-video",action="store_true",help="Explicitly authorized local raw camera video, no audio; bounded asynchronous encoder")
    p.add_argument('--beat-audio',action='store_true',help='Output-only synthesized clicks from published beats; no microphone')
    p.add_argument("--preview-fd", type=int, help=argparse.SUPPRESS)
    p.add_argument("--duration", type=float)
    p.add_argument("--tracking-timeout", type=float, default=2., help="Camera safety stop after this many seconds without reliable tracking; not a Stop gesture")
    p.add_argument("--continuous-test", action="store_true", help="Disable 48-beat score end solely for sustained software tests")
    args = p.parse_args()
    args.calibration_data=None
    if args.calibration is not None:
        if args.camera_index is None:p.error('--calibration is camera-only; videos use explicit --chin-y')
        try:args.calibration_data=load_calibration(args.calibration,args.camera_index,args.width)
        except (ValueError,OSError) as exc:p.error(str(exc)+' No camera was opened.')
        args.chin_y=args.calibration_data['chin_y']
    if args.keyboard and os.name != 'nt':
        p.error('--keyboard is Windows console only; on macOS use launch_rehearsal.py --interactive with line commands')
    if args.startup=='count-in' and args.start_at is not None:
        p.error('count-in cannot be combined with a forced start time')
    MusicalClock(args.initial_bpm,args.meter)
    if args.video is not None and not args.video.is_file():
        p.error("--video must name an existing local file")
    if args.camera_index is not None and (args.camera_index<0 or not args.enable_camera):
        p.error("camera trial requires nonnegative --camera-index and explicit --enable-camera; no camera was opened")
    if args.repeat < 1 or args.width < 256 or args.width > 1920 or args.chin_y is not None and not 0 < args.chin_y < 1:
        p.error("invalid repeat/width/chin calibration")
    if not .5 <= args.tracking_timeout <= 5 or args.duration is not None and not 0 < args.duration <= 600:
        p.error("tracking timeout must be 0.5..5 s; diagnostic duration must be 0..600 s")
    if args.record_video and (args.camera_index is None or args.duration is None):
        p.error("Recording requires camera mode and an explicit bounded diagnostic duration")
    args.output.mkdir(parents=True,exist_ok=False)
    metrics=VideoRun(args).run()
    recording_failed=metrics.get("recording",{}).get("enabled") and (not metrics["recording"].get("finalized") or metrics["recording"].get("error"))
    if recording_failed or metrics["errors"] or metrics["publisher_error"] or metrics.get('audio',{}).get('error') or args.transport=="tcp" and metrics["software_stop_ack"] is None:
        raise SystemExit(1)
