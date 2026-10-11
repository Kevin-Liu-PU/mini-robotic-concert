"""OpenCV dashboard; rendering is local and never saves camera frames."""
import math
import cv2
import numpy as np
from .trajectory import trail_opacity

BG = (25, 21, 17)
CARD = (43, 35, 28)
WHITE = (239, 236, 225)
MUTED = (172, 158, 141)
TEAL = (192, 211, 90)
ORANGE = (84, 171, 255)
PURPLE = (230, 147, 218)
CONNECTIONS = [(0,1),(1,2),(2,3),(3,4),(0,5),(5,6),(6,7),(7,8),
               (5,9),(9,10),(10,11),(11,12),(9,13),(13,14),(14,15),(15,16),
               (13,17),(0,17),(17,18),(18,19),(19,20)]


def text(canvas, label, xy, scale=0.55, color=WHITE, thickness=1):
    cv2.putText(canvas, str(label), xy, cv2.FONT_HERSHEY_SIMPLEX, scale, color,
                thickness, cv2.LINE_AA)


def render(frame, engine, metro, now, trajectory, beat_times, cfg, info, hand=None, tracks=None):
    # A single history remains accepted for older synthetic rendering scripts.
    histories = trajectory if isinstance(trajectory,dict) else {"A":trajectory}
    colors = {"A":TEAL,"B":PURPLE}
    canvas = np.full((800, 1360, 3), BG, np.uint8)
    text(canvas, "HUMAN CONDUCTOR", (28, 42), 0.95, WHITE, 2)
    text(canvas, "INDEX FINGERTIP + QUARTER-NOTE BEATS", (28, 70), 0.51, MUTED)
    text(canvas, 'SINGLE-HAND MODE: SHOW ONLY ONE HAND' if cfg.get('num_hands',2)==1 else
         'TWO-HAND MODE: A/B TRACKS', (480,70), .43, ORANGE if cfg.get('num_hands',2)==1 else MUTED)
    recording=info.get('recording')
    text(canvas, "SYNTHETIC DEMO" if info["demo"] else
         "LOCAL CAMERA / OPT-IN RECORDING" if recording else "LOCAL CAMERA / NO RECORDING", (942, 43), 0.45, TEAL)
    if recording:
        state=recording['state']
        message=(f"REC {recording['remaining_s']:.1f}s  |  NO AUDIO" if state=='RECORDING' else
                 ('Recording READY - press C' if info.get('recording_source_ready',True) else info.get('recording_wait_message') or 'Recording READY - waiting for camera') if state=='READY' else
                 (f"COUNTDOWN {math.ceil(recording['countdown_remaining_s'])} - Q cancels" if recording['countdown_remaining_s']>0 else 'COUNTDOWN done - waiting for fresh frame') if state=='COUNTDOWN' else
                 'REC ARMED - waiting for fresh frame' if state=='ARMED' else
                 'REC FLUSHING - do not close forcibly' if state=='FLUSHING' else
                 'ERROR - check recording / final status' if state=='ERROR' else
                 'STOPPED - no video recorded' if state=='CANCELLED' else 'SAVED LOCALLY - recording stopped')
        cv2.rectangle(canvas,(916,51),(1334,84),(35,30,95),-1)
        text(canvas,message,(925,73),.43,WHITE,1)
        if recording.get('metadata_warning'):
            text(canvas,'STATUS WARNING - check final-status.json',(925,93),.32,ORANGE)
        elif recording['dropped_frames'] or recording['diagnostic_drops']:
            text(canvas,f"REC LOSS: {recording['dropped_frames']} frames / {recording['diagnostic_drops']} diagnostics",(925,93),.34,ORANGE)
    vx, vy, vw, vh = 28, 96, 864, 486
    preview = np.full((vh, vw, 3), CARD, np.uint8)
    # Letterbox while preserving the normalized coordinate mapping to the image.
    if frame is not None:
        scale = min(vw / frame.shape[1], vh / frame.shape[0])
        iw, ih = round(frame.shape[1] * scale), round(frame.shape[0] * scale)
        ox, oy = (vw-iw)//2, (vh-ih)//2
        preview[oy:oy+ih, ox:ox+iw] = cv2.resize(frame, (iw, ih))
    else:
        iw, ih, ox, oy = vw, vh, 0, 0
        if not info["demo"]:
            starting=info.get('source_starting',False)
            heading=('STARTUP FAILED' if info.get('source_fault') else 'STARTING CAMERA / MODEL') if starting else 'CAMERA FRAME UNAVAILABLE'
            text(preview,heading,(100,170),0.8,ORANGE,2)
            text(preview,info.get('source_fault') or info['camera_status'],(70,212),0.53,WHITE)
            text(preview,"Waiting for fresh frames; stale images are hidden.",(70,245),0.5,MUTED)
    def point(x, y):
        return (ox + int(x*iw), oy + int(y*ih))
    # One visible target with a subtle contact tolerance, not two distant gates.
    band = preview.copy()
    cv2.rectangle(band,point(0,engine.config.contact_y),
                  point(1,engine.config.beat_y+engine.config.line_tolerance),ORANGE,-1)
    preview = cv2.addWeighted(band,.16,preview,.84,0)
    a,b = point(0,engine.config.beat_y),point(1,engine.config.beat_y)
    cv2.line(preview,a,b,ORANGE,2,cv2.LINE_AA)
    cv2.rectangle(preview,(8,35),(625,60),BG,-1)
    text(preview,f"QUARTER-NOTE BEAT LINE  {engine.config.beat_y:.0%}  |  W up / S down",
         (16,53),.46,ORANGE)
    text(preview,"Move naturally up/down near center; keep the index-tip dot visible",(16,26),.44,WHITE)
    text(preview,"Comfortable hand pose. Down to line = one beat; lift to prepare.",(16,79),.42,WHITE)
    hands = {slot:track.hand for slot,track in (tracks or {}).items()}
    if hand:
        hands.setdefault("A",hand)
    for slot,observed in hands.items():
        if observed.landmarks:
            def drawable(p):
                try:
                    return all(math.isfinite(v) and 0<=v<=1 for v in (p.x,p.y))
                except (AttributeError,TypeError,ValueError):
                    return False
            for a,b in CONNECTIONS:
                if max(a,b)>=len(observed.landmarks):
                    continue
                p,q = observed.landmarks[a],observed.landmarks[b]
                if drawable(p) and drawable(q):
                    cv2.line(preview,point(p.x,p.y),point(q.x,q.y),colors[slot],1,cv2.LINE_AA)
            for p in observed.landmarks:
                if drawable(p):
                    cv2.circle(preview,point(p.x,p.y),2,MUTED,-1,cv2.LINE_AA)
    for slot,history in histories.items():
        for p,q in history.edges(now,cfg["trajectory_seconds"]):
            fade = trail_opacity(now,q.t,cfg["trajectory_seconds"])
            color = tuple(int(c*fade) for c in colors[slot])
            cv2.line(preview,point(p.x,p.y),point(q.x,q.y),color,3,cv2.LINE_AA)
        current = history.current(now)
        if current:
            pos = point(current.x,current.y)
            cv2.circle(preview,pos,5,colors[slot],-1,cv2.LINE_AA)
            label = f"{slot} MAIN TIP" if info.get("primary")==slot else f"{slot} TIP"
            text(preview,label,(max(8,min(pos[0]+15,vw-170)),max(45,min(pos[1]-16,vh-35))),.50,colors[slot],2)
    if engine.raw:
        cv2.circle(preview,point(engine.raw.x,engine.raw.y),8,ORANGE,-1,cv2.LINE_AA)
        cv2.circle(preview,point(engine.raw.x,engine.raw.y),12,WHITE,2,cv2.LINE_AA)
    canvas[vy:vy+vh, vx:vx+vw] = preview
    trail_label='TIP TRAIL  A: TEAL' if cfg.get('num_hands',2)==1 else 'TIP TRAILS  A: TEAL  B: PURPLE'
    text(canvas,f"{trail_label}  |  LAST {cfg['trajectory_seconds']:g}s  |  ORANGE DOT = MAIN",(44,565),0.40,MUTED)
    px = 920
    cv2.rectangle(canvas, (px,96), (1332,267), CARD, -1)
    holding=engine.tempo_state=="HOLDING / STALE"
    text(canvas, "HOLDING / STALE TEMPO" if holding else "ESTIMATED TEMPO", (px+22,126), 0.54, ORANGE if holding else MUTED)
    text(canvas, "--" if engine.output_bpm is None else f"{engine.output_bpm:.1f}", (px+20,196), 1.75, ORANGE if holding else WHITE, 3)
    text(canvas, "BPM", (px+295,193), 0.75, MUTED)
    text(canvas, ("Prior estimate; awaiting fresh interval" if holding else
                  f"Consistency: {engine.confidence:.0%} (heuristic)" if engine.bpm else
                  f"Collecting: {len(engine.intervals)} / {engine.config.min_intervals} valid intervals"), (px+22,230), 0.49, ORANGE if holding else TEAL)
    text(canvas, f"Valid intervals: {len(engine.intervals)} / {engine.config.window_intervals}", (px+22,251), 0.45, MUTED)
    for top, label, lit, color, sub in (
        (285,f"MAIN {info.get('primary') or '--'} / QUARTER-NOTE BEAT",bool(engine.last_event and now-engine.last_event.t<0.15),ORANGE,f"Current segment beats: {engine.count}"),
        (430,"ESTIMATED-TEMPO METRONOME",metro.running and metro.phase<0.18,ORANGE if holding else TEAL,"Free-running / NO phase following")):
        cv2.rectangle(canvas, (px,top), (1332,top+127), CARD, -1)
        text(canvas,label,(px+18,top+29),0.50,MUTED)
        cv2.circle(canvas,(px+47,top+76),23,color if lit else (73,62,51),-1,cv2.LINE_AA)
        text(canvas,"BEAT" if top==285 else ("HOLDING / STALE" if holding else "RUNNING" if metro.running else "WAITING"),(px+90,top+73),0.60,color)
        text(canvas,sub,(px+90,top+104),0.43,MUTED)
    text(canvas,"INDEX TIP HEIGHT / LAST 8 SECONDS",(28,612),0.48,MUTED)
    gx,gy,gw,gh=28,627,864,91
    cv2.rectangle(canvas,(gx,gy),(gx+gw,gy+gh),CARD,-1)
    cv2.line(canvas,(gx,gy+int(gh*engine.config.beat_y)),
             (gx+gw,gy+int(gh*engine.config.beat_y)),ORANGE,1)
    for slot,history in histories.items():
        for p,q in history.edges(now,8):
            a=(gx+int(gw*(1-(now-p.t)/8)),gy+int(gh*p.y))
            b=(gx+int(gw*(1-(now-q.t)/8)),gy+int(gh*q.y))
            cv2.line(canvas,a,b,colors[slot],1,cv2.LINE_AA)
    for stamp in beat_times:
        if 0<=now-stamp<=8:
            x=gx+int(gw*(1-(now-stamp)/8))
            cv2.line(canvas,(x,gy),(x,gy+gh),ORANGE,1)
    # Presentation-only wording: detector states and recording states stay intact.
    status=engine.status if info.get("primary") else info.get("primary_status",engine.status)
    if info.get("primary"):
        status={
            "READY - DOWN TO BEAT LINE": "Beat armed - move down to touch / cross line",
            "LIFT FROM LINE TO READY": "Return above line to prepare the next beat",
            "ACQUIRING - RETURN UP": "Acquiring hand - return above line to prepare",
            "PAUSED - RETURN UP TO REARM": "Beat paused - return above line to prepare",
        }.get(status,status)
    text(canvas,status[:65],(28,748),0.53,TEAL if engine.smoothed else ORANGE)
    controls=('W/S: line   |   R: clear   |   C: start REC   |   Q/Esc: quit' if cfg.get('num_hands',2)==1 else
              'W/S: line   |   1/2: MAIN   |   R: clear   |   C: start REC   |   Q/Esc: quit')
    text(canvas,controls,(28,778),0.48,MUTED)
    text(canvas,f"Capture: {info['capture_fps']:.1f} FPS",(px,601),0.5,WHITE)
    text(canvas,f"Processed: {info['processing_fps']:.1f} FPS",(px,629),0.5,WHITE)
    text(canvas,f"Inference: {info['inference_ms']:.1f} ms",(px,657),0.5,WHITE)
    text(canvas,info["hand_label"][:43],(px,687),0.44,MUTED)
    text(canvas,info["camera_mode"][:45],(px,716),0.42,MUTED)
    text(canvas,info["camera_name"][:45],(px,738),0.42,TEAL)
    text(canvas,info["camera_status"][:49],(px,756),0.34,MUTED)
    text(canvas,"Tempo only. No audio or robot output.",(px,777),0.45,ORANGE)
    return canvas
