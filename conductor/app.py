"""Local conductor prototype; video stays in RAM unless --record is explicit."""
import argparse
from collections import deque
import json
import math
import re
from pathlib import Path
import sys
import time

from .core import DetectorConfig, TempoMetronome
from .settings import load_config
from .tracking import AssociationConfig, hands_from_result
from .multitrack import ConductorSession
from .synthetic import demo_hand
from .numeric_trace import NumericTrace, frame_record, result_counts


def update_mock_cello(mock, session, sample, event, now):
    """Only the observed session Beat enters the mock, never a clock tick."""
    if mock is not None:
        mock.step(now, sample=sample, event=event,
                  source_token=session.primary_token if session.beat_anchor else None)


def render_mock_cello(canvas, mock):
    """Append a labelled mock strip without covering the existing dashboard."""
    import cv2
    import numpy as np
    strip = np.full((44, canvas.shape[1], 3), (30, 30, 30), np.uint8)
    cv2.putText(strip, mock.label(), (16, 29), cv2.FONT_HERSHEY_SIMPLEX,
                .5, (90, 220, 255), 1, cv2.LINE_AA)
    return np.vstack((canvas, strip))


def adjust_line_for_key(session, key, now, screen_clock=None):
    """W/S move the line by 1% of image height, within a usable image region."""
    if key not in (ord("w"), ord("W"), ord("s"), ord("S")):
        return False
    config = session.engine.config
    step = -.01 if key in (ord("w"), ord("W")) else .01
    lower = max(.10, config.rearm_distance+.02)
    upper = min(.90, 1-config.line_tolerance-.02)
    height = round(max(lower, min(upper, config.beat_y+step)), 6)
    changed = session.set_beat_line(height, now)
    if changed and screen_clock is not None:
        screen_clock.beat_line_changed(now)
    return changed


def audio_control_for_key(audio_output, key, now):
    """Record every received M, including refused presses during startup."""
    if key not in (ord('m'), ord('M')):
        return None
    accepted = bool(audio_output and audio_output.toggle_mute(now))
    state = audio_output.snapshot() if audio_output else {'muted': True, 'fault': None}
    reason = ('MUTED' if state['muted'] else 'SOUND_ENABLED') if accepted else (
        'SOURCE_NOT_READY' if audio_output is None else
        'AUDIO_FAULT' if state.get('fault') else 'AUDIO_NOT_READY')
    return dict(kind='AUDIO_CONTROL', time_s=now, action='TOGGLE_MUTE',
                accepted=accepted, muted=state['muted'], reason=reason)


def publish_report(path,value,*,final=False):
    """Keep transient Windows readers from aborting preview; final failure is explicit."""
    if path is None:return True
    temporary=path.with_suffix(path.suffix+'.tmp')
    temporary.write_text(json.dumps(value,indent=2)+'\n',encoding='utf-8')
    for attempt in range(4 if final else 1):
        if attempt:time.sleep(.02*(2**(attempt-1)))
        try:
            temporary.replace(path)
            return True
        except PermissionError as exc:
            if final and attempt==3:
                print(f'FINAL SESSION REPORT ERROR: {exc}; final data remains at {temporary}',file=sys.stderr,flush=True)
    return False


def create_hand_landmarker(cfg, python_api, vision_api):
    """Pass the configured capacity to the model itself, never trim its output."""
    return vision_api.HandLandmarker.create_from_options(vision_api.HandLandmarkerOptions(
        base_options=python_api.BaseOptions(model_asset_path=cfg['model_path']),
        running_mode=vision_api.RunningMode.VIDEO,num_hands=cfg['num_hands'],
        min_hand_detection_confidence=cfg['min_hand_detection_confidence'],
        min_hand_presence_confidence=cfg['min_hand_presence_confidence'],
        min_tracking_confidence=cfg['min_tracking_confidence']))


def load_hand_model(cfg):
    """Shared real SDK loader for synchronous and background initialization."""
    if not Path(cfg['model_path']).is_file():
        raise FileNotFoundError('Missing model; run setup.cmd or download_model.py')
    import mediapipe as model_mp
    from mediapipe.tasks import python
    from mediapipe.tasks.python import vision
    return model_mp, create_hand_landmarker(cfg, python, vision)


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", help="Path to editable JSON config")
    parser.add_argument("--camera", type=int, help="OpenCV camera index")
    parser.add_argument("--camera-name", help="Unique camera name substring; resolves current backend index")
    parser.add_argument("--backend", choices=["dshow", "msmf", "auto"])
    parser.add_argument("--hand", choices=["any", "Left", "Right"],
                        help="Optional initial MAIN label hint only; 1/2 selects the visible A/B track")
    parser.add_argument('--num-hands',type=int,choices=(1,2),
                        help='Model capacity: 1 requires only one physical hand in view; 2 enables A/B tracking')
    parser.add_argument("--demo", action="store_true", help="Synthetic 60/120 BPM, then pause, repeat; no camera/model")
    parser.add_argument('--mock-cello', action='store_true',
                        help='MOCK only: A then B arms one observed beat; X/Space stops; no robot connection')
    parser.add_argument('--hold-clock', action='store_true',
                        help='Opt-in screen clock: hold BPM on hand loss; Space stops, Enter resumes saved BPM or acquires fresh')
    parser.add_argument('--audio-clock', action='store_true',
                        help='Opt-in output-only click stream, initially muted; requires --hold-clock and preview; M toggles sound')
    parser.add_argument('--audio-config', type=Path, help='Separate editable audio JSON; requires --audio-clock')
    parser.add_argument("--headless", action="store_true", help="Process without showing a window")
    parser.add_argument("--seconds", type=float, default=0, help="Stop after this many seconds; 0 means until quit")
    parser.add_argument("--list-cameras", action="store_true", help="List device names/indices without opening camera streams")
    parser.add_argument("--report", type=Path, help="Local aggregate JSON plus event/frame numeric traces, never images/landmarks")
    parser.add_argument("--demo-screenshot", type=Path, help="Save one synthetic-only dashboard PNG on exit")
    parser.add_argument("--record", metavar="NAME", help="Enable local silent recording; press C in preview for a 3-second countdown, then at most 45 seconds")
    parser.add_argument("--record-seconds", type=float, default=45, help="Recording limit, >0 and <=45; only with --record")
    args = parser.parse_args(argv)
    if args.mock_cello and (args.headless or args.list_cameras):
        parser.error('--mock-cello requires interactive preview; for no-device CLI use python -m conductor.mock_cello_demo')
    if args.audio_clock and (not args.hold_clock or args.headless or args.list_cameras):
        parser.error('--audio-clock requires --hold-clock and an interactive preview, without --list-cameras')
    if args.audio_config and not args.audio_clock:
        parser.error('--audio-config requires --audio-clock')
    if args.record and (not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,63}',args.record) or args.list_cameras):
        parser.error("--record needs a simple new local run name and cannot be combined with --list-cameras")
    if not math.isfinite(args.record_seconds) or not 0<args.record_seconds<=45:
        parser.error("--record-seconds must be >0 and <=45")
    if not args.record and args.record_seconds!=45:
        parser.error("--record-seconds requires --record")
    if args.record and args.headless:
        parser.error("--record requires the preview window and an explicit C key to start")
    if args.demo_screenshot and not args.demo:
        parser.error("--demo-screenshot requires --demo; camera screenshots are not supported")
    if args.seconds < 0 or (args.camera is not None and args.camera < 0):
        parser.error("seconds and camera must be nonnegative")
    if args.headless and not args.list_cameras and args.seconds <= 0:
        parser.error("headless mode requires a positive --seconds limit")
    return args


def create_screen_clock(cfg, audio_config=None):
    """Choose a reversible policy before initializing any devices."""
    from .screen_clock import ScreenClock, ScreenClockConfig
    policy = cfg.get('follow_policy', 'baseline')
    phase = cfg.get('phase_policy', 'legacy')
    if policy not in ('baseline', 'responsive'):
        raise ValueError('follow_policy must be baseline or responsive')
    if phase not in ('legacy', 'reentry') or (phase == 'reentry' and policy != 'responsive'):
        raise ValueError('phase_policy reentry requires responsive following')
    config = ScreenClockConfig(**cfg.get('screen_clock', {}))
    if phase == 'reentry':
        from .phase_clock import ReentryResponsiveScreenClock
        # Protect the full allowed driver lead, not merely its nominal latency.
        horizon = audio_config.max_output_lead_s if audio_config is not None else .20
        return ReentryResponsiveScreenClock(config, commit_horizon_s=horizon)
    if policy == 'responsive':
        from .responsive_clock import ResponsiveScreenClock
        return ResponsiveScreenClock(config)
    return ScreenClock(config)


def main():
    args = parse_args()
    mock_cello = None
    if args.mock_cello:
        from .mock_cello import MockCelloControls
        mock_cello = MockCelloControls()
    cfg = load_config(args.config)
    audio_config = None
    if args.audio_clock:
        from .audio_clock import AudioConfig
        path = args.audio_config or Path(__file__).resolve().parents[1]/'config-audio.json'
        audio_config = AudioConfig(**json.loads(path.read_text(encoding='utf-8-sig')))
    screen_clock = clock_trace = None
    audio_output = audio_trace = None
    last_audio_control = None
    audio_control_count = 0
    if args.hold_clock:
        from .screen_display import render_clock
        screen_clock = create_screen_clock(cfg, audio_config)
        if args.report is None and not args.record:
            folder = Path(__file__).resolve().parents[1]/'logs'
            folder.mkdir(exist_ok=True)
            args.report = folder/f'screen-hold-{time.time_ns()}.json'
    for argument, key in ((args.camera,"camera_index"),(args.backend,"camera_backend"),(args.hand,"hand"),
                          (args.num_hands,'num_hands')):
        if argument is not None:
            cfg[key] = argument
    import cv2
    import numpy as np
    from .capture import Camera, rate
    from .display import render
    if args.list_cameras:
        from .devices import list_devices
        print(json.dumps(list_devices(cfg["camera_backend"]),indent=2))
        return 0
    if not args.demo:
        from .devices import resolve_camera
        name=args.camera_name or (cfg.get("camera_name") if args.camera is None else None)
        cfg=resolve_camera(cfg,name)
        print(f"Selected camera: {cfg['camera_name']} (index {cfg['camera_index']}, {cfg['camera_backend']})",flush=True)
    session = ConductorSession(DetectorConfig(**cfg["detector"]),
                               AssociationConfig(**cfg.get("association",{})),
                               cfg["trajectory_seconds"],cfg["hand"])
    engine = session.engine
    metronome = TempoMetronome()
    camera = landmarker = model_startup = None
    recording = None
    frame_trace = None
    window = "Human Conductor - experimental audio clock" if args.audio_clock else "Human Conductor - stages 1 and 2"
    beats = deque()
    process_times = deque(maxlen=180)
    inference_times = deque(maxlen=600)
    processed = detected = 0
    bpm_values = deque(maxlen=3600)
    observed_strokes = 0
    last_sequence = last_model_ms = -1
    shown_frame = None
    hand = None
    inference_ms = 0.0
    start = time.perf_counter_ns()/1e9
    if screen_clock:
        screen_clock.begin_startup(start)
    last_processed = None
    canvas = None
    report = {}
    preview_validation = {}
    previous_preview_sample = None
    exit_reason = "unknown"
    last_report_time = 0.0
    loop_marks = []
    previous_loop_marks = []
    event_path=args.report.with_suffix(".events.jsonl") if args.report else None
    def save_clock_events():
        if screen_clock:
            events = screen_clock.drain_events()
            if audio_output:
                audio_output.sync(screen_clock, time.perf_counter(), events)
            for item in events:
                # Separate numeric journal. Never append predictions to detector events.
                item['clock_origin_monotonic_s'] = start
                if clock_trace:
                    clock_trace.write(item)
                    if item['kind']=='SYSTEM_FAULT' and item.get('reason')=='PROCESSING_HEARTBEAT_GAP':
                        clock_trace.write(dict(kind='LOOP_GAP_CONTEXT',time_s=item['time_s'],
                            previous_loop=previous_loop_marks,current_loop=loop_marks,
                            clock_origin_monotonic_s=start))
            if audio_output and audio_trace:
                for item in audio_output.drain_events():
                    item['clock_origin_monotonic_s'] = start
                    audio_trace.write(item)
    def save_events():
        events=engine.drain_events()
        if event_path and events:
            with event_path.open("a",encoding="utf-8") as output:
                for item in events:
                    stamp=item.pop("t")
                    item["session_time_s"]=round(stamp-start,6) if stamp is not None else 0.0
                    if "beat_t" in item:
                        item["beat_time_s"]=round(item.pop("beat_t")-start,6)
                    output.write(json.dumps(item)+"\n")
    def audio_state():
        state = audio_output.snapshot() if audio_output else dict(
            enabled=bool(args.audio_clock), state='WAITING FOR SOURCE' if args.audio_clock else 'DISABLED',
            muted=True, playback_requests=0)
        return dict(state, last_control=last_audio_control, control_count=audio_control_count)

    def snapshot(running, reason=None):
        now=time.perf_counter()
        return {"mode":"synthetic_demo" if args.demo else "live_camera","running":running,"exit_reason":reason,
                "duration_s":round(now-start,2),"camera_index":None if args.demo else cfg["camera_index"],
                "camera_name":None if args.demo else cfg.get("camera_name"),
                "camera_backend":None if args.demo else cfg["camera_backend"],
                "requested_mode":f"{cfg['width']}x{cfg['height']} @ {cfg['requested_fps']} FPS",
                "camera":camera.stats() if camera else None,"processed_frames":processed,
                "frames_with_selected_hand":detected,"detected_strokes":observed_strokes,
                "processing_fps_recent":round(rate(process_times),2) if last_processed and now-last_processed<1 else 0,
                "inference_ms_median":round(float(np.median(inference_times)),2) if inference_times else None,
                "final_bpm":engine.bpm,"bpm_min":min(bpm_values) if bpm_values else None,
                "display_bpm":engine.output_bpm,"tempo_state":engine.tempo_state,
                "selection_reason":session.tracker.last_reason,"selection_details":session.details(),
                "association_policy":"bounded_ownership_and_full_geometry_overlap_v3",
                "num_hands":cfg['num_hands'],
                "hand_mode":"single_hand_only" if cfg['num_hands']==1 else "two_hand_tracks",
                "numeric_frame_trace_records":frame_trace.records if frame_trace else 0,
                "inference_median_window_max_calls":600,
                "model_options":None if args.demo else {"running_mode":"VIDEO","num_hands":cfg['num_hands'],
                    "inference_width":cfg["inference_width"],
                    "min_hand_detection_confidence":cfg["min_hand_detection_confidence"],
                    "min_hand_presence_confidence":cfg["min_hand_presence_confidence"],
                    "min_tracking_confidence":cfg["min_tracking_confidence"]},
                "association_config":vars(session.config),
                "primary_track":session.primary,"visible_tracks":len(session.tracker.tracks),
                "bpm_max":max(bpm_values) if bpm_values else None,"final_status":engine.status,
                "valid_intervals":len(engine.intervals),"required_intervals":engine.config.min_intervals,
                "strokes_since_reset":engine.count,"diagnostic_counts":dict(engine.diagnostic_counts),
                "beat_convention":"one_quarter_note_per_prepared_downward_line_contact",
                "beat_anchor":"index_fingertip_landmark_8","beat_line_height":engine.config.beat_y,
                "line_tolerance":engine.config.line_tolerance,"rearm_distance":engine.config.rearm_distance,
                "camera_frames_saved_or_uploaded":bool(recording and not args.demo and (recording.status()['video_file_created'] or recording.status()['encoded_frames'])),
                "camera_frames_uploaded":False,
                "recording":recording.status() if recording else None,
                "phase_synchronization":False,
                "screen_clock":screen_clock.snapshot(now) if screen_clock else None,
                "screen_clock_policy":screen_clock.snapshot(now).get('follow_policy',
                    'hold_last_tempo_manual_resume_v2') if screen_clock else None,
                "audio_clock":audio_state(),
                "mock_cello":mock_cello.snapshot() if mock_cello else None,
                "preview_validation":preview_validation}
    def save_report(value,final=False):
        return publish_report(args.report,value,final=final)
    try:
        if args.record:
            from .recording import LocalRecording
            from .recording_diagnostics import detector_state,diagnostic_record
            folder=Path(__file__).resolve().parents[1]/'recordings'/args.record
            recording=LocalRecording(folder,start,duration=args.record_seconds,
                scope='synthetic_dashboard' if args.demo else 'source_camera_unmirrored',
                mirror_display=cfg['mirror'],manual_start=True)
            if args.report is None:
                args.report=folder/'session.json'
                event_path=args.report.with_suffix('.events.jsonl')
            print(f"LOCAL RECORDING ENABLED: {folder} | wait for Recording READY, then press C for a 3-second countdown and {args.record_seconds:g}s REC | no audio, no desktop, no upload",flush=True)
        if args.report:
            frame_trace = NumericTrace(args.report.with_suffix('.frames.jsonl'))
            if screen_clock:
                clock_trace = NumericTrace(args.report.with_suffix('.clock.jsonl'))
            if args.audio_clock:
                audio_trace = NumericTrace(args.report.with_suffix('.audio.jsonl'))
        if not args.demo and not screen_clock:
            if not Path(cfg["model_path"]).is_file():
                print("Missing model. Run setup.cmd or python download_model.py. Try demo.cmd without a model.",file=sys.stderr)
                return 1
            mp, landmarker = load_hand_model(cfg)
            camera = Camera(cfg)
            if recording:recording.start_source(camera.latest)
        if not args.headless:
            cv2.namedWindow(window, cv2.WINDOW_NORMAL)
            cv2.resizeWindow(window, 1360, 800)
        if not args.demo and screen_clock:
            from .startup import ResourceStartup, camera_fault
            model_startup = ResourceStartup(lambda: load_hand_model(cfg), lambda value: value[1].close())
        while True:
            now = time.perf_counter_ns()/1e9
            loop_marks=[('loop_start',now)]
            elapsed = now-start
            if args.seconds and elapsed >= args.seconds:
                exit_reason = "time_limit"
                break
            sample = None
            event = None
            clock_fault = None
            if model_startup:
                loaded, load_error = model_startup.take()
                if load_error:
                    clock_fault = 'MODEL_INITIALIZATION_FAILED'
                    print('MODEL INITIALIZATION FAILED: '+load_error, file=sys.stderr, flush=True)
                    if getattr(model_startup, 'failure_traceback', None):
                        print(model_startup.failure_traceback, file=sys.stderr, flush=True)
                if loaded is not None:
                    mp, landmarker = loaded
                    camera = Camera(cfg)
                    if recording:
                        recording.start_source(camera.latest)
            recording_diagnostic=None
            recording_sequence=recording_timestamp=None
            if args.demo:
                processing_started = time.perf_counter()
                # Piecewise continuous phase: 60 BPM 0..12s, 120 BPM 12..24s,
                # pause 24..28s; repeats. Demo passes through the exact same core.
                segment = elapsed % 28
                phase = segment if segment < 12 else 12+2*(segment-12)
                line = engine.config.beat_y
                y = line-.14 + .14*math.cos(2*math.pi*phase) if segment < 24 else None
                demo_hands = [demo_hand(.25,y,"Right")] if y is not None else []
                # A phase-offset secondary wrist demonstrates that two visible
                # tracks still supply only one tempo stream. No camera is used.
                if cfg['num_hands']==2 and 3 < segment < 24:
                    demo_hands.append(demo_hand(.75,line-.14+.14*math.cos(2*math.pi*(phase+.5)),"Left"))
                before_state=detector_state(engine) if recording else None
                event = session.update(demo_hands,now)
                if recording:
                    recording_sequence,recording_timestamp=processed+1,now
                    recording_diagnostic=diagnostic_record(session,demo_hands,recording_sequence,now,start,before_state)
                hand = session.hand
                anchor = session.beat_anchor
                sample = (now,anchor[0] if anchor else None,anchor[1] if anchor else None)
                shown_frame = np.full((540,960,3),(43,35,28),np.uint8)
                completed = time.perf_counter()
                if frame_trace:
                    duration_ms=(completed-processing_started)*1000
                    frame_trace.write(frame_record(session,start=start,timestamp=now,
                        completed_at=completed,sequence=processed+1,synthetic=True,accepted=True,
                        inference_ms=None,preparation_ms=None,association_ms=None,processing_ms=duration_ms,
                        frame_age_at_start_ms=0,counts=dict(model_candidate_count=None,
                        model_valid_wrists=None,model_valid_tips=None,synthetic_candidate_count=len(demo_hands))))
            else:
                packet = camera.latest() if camera else None
                if packet and packet[0] != last_sequence:
                    sequence, timestamp, frame = packet
                    last_sequence = sequence
                    stamp_ms = int((timestamp-start)*1000)
                    # Skip duplicate millisecond stamps rather than invent time.
                    if stamp_ms > last_model_ms and now-timestamp <= engine.config.max_frame_gap_s:
                        processing_started = time.perf_counter()
                        last_model_ms = stamp_ms
                        view = cv2.flip(frame,1) if cfg["mirror"] else frame
                        height = round(view.shape[0]*cfg["inference_width"]/view.shape[1])
                        small = cv2.resize(view,(cfg["inference_width"],height))
                        if screen_clock:
                            # Numeric local-only evidence of nonblack, changing source pixels.
                            # Keep this tiny sampled array in RAM; never save or transmit it.
                            probe = small[::16, ::16].astype(np.float32)
                            delta = (float(np.mean(np.abs(probe-previous_preview_sample)))
                                     if previous_preview_sample is not None and probe.shape == previous_preview_sample.shape else None)
                            previous_preview_sample = probe
                            preview_validation = dict(source_sequence=sequence,
                                source_time_s=timestamp-start, pixel_mean=float(np.mean(probe)),
                                pixel_std=float(np.std(probe)), pixel_min=float(np.min(probe)),
                                pixel_max=float(np.max(probe)), mean_absolute_change=delta,
                                images_saved_or_transmitted=False)
                        image = mp.Image(image_format=mp.ImageFormat.SRGB,data=cv2.cvtColor(small,cv2.COLOR_BGR2RGB))
                        before = time.perf_counter_ns()
                        preparation_ms = (before/1e9-processing_started)*1000
                        result = landmarker.detect_for_video(image,stamp_ms)
                        inference_finished = time.perf_counter_ns()/1e9
                        inference_ms = (inference_finished-before/1e9)*1000
                        inference_times.append(inference_ms)
                        before_state=detector_state(engine) if recording else None
                        accepted,event = session.consume_result(result,timestamp,inference_finished)
                        if screen_clock and screen_clock.first_valid_frame_at is not None and not accepted and session.last_result_status == 'inference_result_too_old':
                            clock_fault = 'INFERENCE_RESULT_TOO_OLD'
                        if recording:
                            recording_diagnostic=diagnostic_record(session,hands_from_result(result),sequence,timestamp,start,before_state,accepted)
                            recording.add_diagnostic(recording_diagnostic)
                        completed = time.perf_counter()
                        if frame_trace:
                            frame_trace.write(frame_record(session,start=start,timestamp=timestamp,
                                completed_at=completed,sequence=sequence,synthetic=False,accepted=accepted,
                                inference_ms=inference_ms,preparation_ms=preparation_ms,
                                association_ms=(completed-inference_finished)*1000,
                                processing_ms=(completed-processing_started)*1000,
                                frame_age_at_start_ms=(processing_started-timestamp)*1000,
                                counts=result_counts(result)))
                        hand = session.hand
                        shown_frame = small if accepted else None
                        if accepted:
                            anchor = session.beat_anchor
                            sample = (timestamp,anchor[0] if anchor else None,anchor[1] if anchor else None)
                if (not packet or now-packet[1] > engine.config.max_frame_gap_s):
                    session.update([],now,missing_reason="stale_camera_frame")
                    hand = None
                    shown_frame = None
            loop_marks.append(('source_and_frame_journal_finished',time.perf_counter()))
            if sample:
                timestamp,x,y = sample
                processed += 1
                completed_at=time.perf_counter_ns()/1e9
                if process_times and completed_at-process_times[-1]>engine.config.max_frame_gap_s:
                    process_times.clear()
                process_times.append(completed_at)
                last_processed = timestamp
                if x is not None and y is not None:
                    detected += 1
                if event:
                    beats.append(event.t)
                    observed_strokes += 1
                if engine.bpm:
                    bpm_values.append(engine.bpm)
            now = time.perf_counter_ns()/1e9
            engine.expire(now)
            update_mock_cello(mock_cello, session, sample, event, now)
            save_events()
            metronome.update(now,engine.output_bpm)
            loop_marks.append(('event_journal_finished',time.perf_counter()))
            if args.report and now-last_report_time>=2:
                save_report(snapshot(True))
                last_report_time=now
            loop_marks.append(('report_finished',time.perf_counter()))
            for trajectory in session.trajectories.values():
                trajectory.prune(now)
            while beats and now-beats[0] > 8:
                beats.popleft()
            camera_stats = camera.stats() if camera else {}
            if screen_clock:
                now = time.perf_counter_ns()/1e9
                loop_marks.append(('before_clock_update',now))
                if audio_output and audio_output.fault:
                    clock_fault = clock_fault or audio_output.fault
                if camera:
                    clock_fault = clock_fault or camera_fault(camera_stats, screen_clock.first_valid_frame_at is not None)
                clock_state = screen_clock.step(now, frame_time=sample[0] if sample else None,
                    track_good=bool(sample and sample[1] is not None and sample[2] is not None),
                    beat_time=event.t if event else None,
                    camera_time=(packet[1] if packet else None) if camera else (sample[0] if sample else None),
                    fault=clock_fault)
                if (args.audio_clock and audio_output is None and clock_state['source_initialized']
                        and clock_state['healthy'] and not clock_state['fault']):
                    from .audio_output import AudioOutput
                    audio_output = AudioOutput(audio_config)
                save_clock_events()
                loop_marks.append(('clock_and_audio_finished',time.perf_counter()))
            from .startup import source_status_labels
            source_starting = bool(screen_clock and screen_clock.first_valid_frame_at is None)
            source_fault = screen_clock.fault_reason if screen_clock else None
            camera_mode, camera_status = source_status_labels(args.demo, camera_stats, source_starting, source_fault)
            info = {"demo":args.demo,"capture_fps":camera_stats.get("capture_fps_recent",0),
                    "processing_fps":rate(process_times) if last_processed and now-last_processed<1 else 0,
                    "inference_ms":inference_ms,
                    "hand_label":(f"MAIN {session.primary} | single hand" if cfg['num_hands']==1 and
                                  session.primary and not session.anchor_missing else session.display_status()),
                    "primary":session.primary,
                    "primary_status":session.display_status(),
                    "camera_mode":camera_mode,
                    "camera_status":camera_status,
                    "camera_name":"Synthetic input" if args.demo else cfg.get("camera_name","Unknown camera")}
            info['recording']=recording.status() if recording else None
            info['source_starting']=source_starting
            info['source_fault']=source_fault
            recording_source_ready=bool(args.demo or (shown_frame is not None and camera_stats.get('fresh_frame_age_s',1)<=engine.config.max_frame_gap_s))
            if screen_clock:
                from .startup import recording_gate
                recording_source_ready, info['recording_wait_message'] = recording_gate(clock_state, recording_source_ready)
            info['recording_source_ready']=recording_source_ready
            if not args.headless or args.demo_screenshot or (recording and args.demo):
                canvas = render(shown_frame,engine,metronome,now,session.trajectories,beats,cfg,info,
                                tracks=session.tracker.tracks)
                if screen_clock:
                    canvas = render_clock(canvas, clock_state, now, recording_enabled=recording is not None,
                                          audio_state=audio_state() if args.audio_clock else None,
                                          gesture_state=engine.feedback(now))
                if mock_cello:
                    canvas = render_mock_cello(canvas, mock_cello)
            loop_marks.append(('render_finished',time.perf_counter()))
            if recording and args.demo:
                recording.submit(recording_sequence,recording_timestamp,canvas)
                recording.add_diagnostic(recording_diagnostic)
            if not args.headless:
                cv2.imshow(window,canvas)
                loop_marks.append(('present_finished',time.perf_counter()))
                key = cv2.waitKey(1)&0xFF
                loop_marks.append(('key_poll_finished',time.perf_counter()))
                if mock_cello:
                    mock_cello.key(key, time.perf_counter())
                if key in (27,ord("q"),ord("Q")):
                    exit_reason = "quit_key"
                    break
                if cv2.getWindowProperty(window,cv2.WND_PROP_VISIBLE)<1:
                    exit_reason = "window_closed"
                    break
                if recording and key in (ord('c'),ord('C')):
                    recording.request_start(recording_source_ready)
                if args.audio_clock and key in (ord('m'),ord('M')):
                    last_audio_control=audio_control_for_key(audio_output,key,time.perf_counter())
                    audio_control_count+=1
                    if audio_trace:
                        audio_trace.write(dict(last_audio_control,clock_origin_monotonic_s=start))
                if key in (ord("1"),ord("2")):
                    if screen_clock:
                        screen_clock.stop(time.perf_counter(), 'MAIN_SELECTION_CHANGED')
                    session.select_primary("A" if key==ord("1") else "B")
                    metronome.update(time.perf_counter(),engine.output_bpm)
                if adjust_line_for_key(session,key,time.perf_counter(),screen_clock):
                    metronome=TempoMetronome()
                    beats.clear()
                if key in (ord("r"),ord("R")):
                    if screen_clock:
                        screen_clock.reset(time.perf_counter())
                    session.reset(reason="manual_reset")
                    metronome=TempoMetronome()
                    beats.clear()
                    hand=shown_frame=None
                if screen_clock and key == 32:
                    screen_clock.stop(time.perf_counter())
                if screen_clock and key in (10, 13):
                    if (not audio_output or not audio_output.fault) and screen_clock.arm(time.perf_counter()):
                        session.reset(reason='screen_clock_rearm')
                        beats.clear()
                save_clock_events()
            loop_marks.append(('controls_finished',time.perf_counter()))
            time.sleep(1/60 if args.demo else 0.002)
            loop_marks.append(('loop_end',time.perf_counter()))
            previous_loop_marks=loop_marks
        if screen_clock:
            screen_clock.stop(time.perf_counter(), 'APPLICATION_EXIT')
            save_clock_events()
        if mock_cello:
            mock_cello.stop('APPLICATION_EXIT')
        report = snapshot(False,exit_reason)
        save_events()
    except Exception as exc:
        if mock_cello:
            mock_cello.bow.fail(f'APPLICATION_ERROR: {type(exc).__name__}: {exc}')
        if screen_clock:
            screen_clock.fail(time.perf_counter(), 'APPLICATION_ERROR')
            print(f'SCREEN CLOCK STOPPED: {type(exc).__name__}: {exc}', file=sys.stderr, flush=True)
            save_clock_events()
            save_report(snapshot(False, 'application_error'), final=True)
        raise
    finally:
        if mock_cello:
            mock_cello.stop('APPLICATION_EXIT')
            report['mock_cello'] = mock_cello.snapshot()
        if audio_output:
            report['audio_closed_cleanly'] = audio_output.close()
            if audio_trace:
                for item in audio_output.drain_events():
                    item['clock_origin_monotonic_s'] = start
                    audio_trace.write(item)
            report['audio_clock'] = audio_state()
        if audio_trace:
            audio_trace.close()
        if model_startup:
            model_startup.close()
        if screen_clock and screen_clock.armed:
            screen_clock.stop(time.perf_counter(), 'APPLICATION_EXIT')
            save_clock_events()
        if clock_trace:
            clock_trace.close()
        if recording:recording.stop(exit_reason if exit_reason!='unknown' else 'application_error')
        if camera:
            report["camera_closed_cleanly"] = camera.close()
        if recording:
            report['recording_closed_cleanly']=recording.close(exit_reason if exit_reason!='unknown' else 'application_error')
            report['recording']=recording.status()
            report['camera_frames_saved_or_uploaded']=bool(not args.demo and (recording.status()['video_file_created'] or recording.status()['encoded_frames']))
            report['camera_frames_uploaded']=False
        if frame_trace:
            frame_trace.close()
        if landmarker:
            landmarker.close()
        if not args.headless:
            cv2.destroyAllWindows()
    if args.demo_screenshot and canvas is not None:
        if not cv2.imwrite(str(args.demo_screenshot),canvas):
            raise RuntimeError("Could not save synthetic dashboard image")
    print(json.dumps(report,indent=2),flush=True)
    report_saved=save_report(report,final=True)
    return 1 if (not report_saved or (recording and not report.get('recording_closed_cleanly',False))
                 or (audio_output and (audio_output.fault or not report.get('audio_closed_cleanly',False)))) else 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (ValueError,FileNotFoundError,ImportError) as exc:
        print(f"Setup/configuration error: {exc}",file=sys.stderr)
        sys.exit(1)
