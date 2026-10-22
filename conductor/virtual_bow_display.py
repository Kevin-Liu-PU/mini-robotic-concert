"""Local virtual-bow canvas; consumes existing observed fingertip trajectories."""
import math
import cv2
import numpy as np
from .display import BG, CARD, WHITE, MUTED, TEAL, PURPLE, ORANGE, text
from .trajectory import trail_opacity


def render_virtual_bow(frame, session, bow, now, cfg, info):
    canvas = np.full((800, 1360, 3), BG, np.uint8)
    text(canvas, 'TWO-HAND VIRTUAL BOW', (28, 42), .9, WHITE, 2)
    text(canvas, 'SCREEN SIMULATION ONLY | NO ROBOT | NO TRIANGLE RECOGNITION', (28, 74), .49, ORANGE)
    text(canvas, 'SYNTHETIC INPUT' if info['demo'] else 'LOCAL CAMERA', (1030, 42), .5, TEAL)
    vx, vy, vw, vh = 28, 106, 864, 486
    preview = np.full((vh, vw, 3), CARD, np.uint8)
    iw, ih, ox, oy = vw, vh, 0, 0
    if frame is not None:
        scale = min(vw/frame.shape[1], vh/frame.shape[0])
        iw, ih = round(frame.shape[1]*scale), round(frame.shape[0]*scale)
        ox, oy = (vw-iw)//2, (vh-ih)//2
        preview[oy:oy+ih, ox:ox+iw] = cv2.resize(frame, (iw, ih))
    else:
        text(preview, 'Waiting for a fresh camera frame', (65, 180), .7, ORANGE)
    def point(x, y):
        return ox+round(x*iw), oy+round(y*ih)
    for slot, trail in session.trajectories.items():
        color = TEAL if slot == 'A' else PURPLE
        for a, b in trail.edges(now, cfg['trajectory_seconds']):
            fade = trail_opacity(now, b.t, cfg['trajectory_seconds'])
            cv2.line(preview, point(a.x, a.y), point(b.x, b.y), tuple(int(c*fade) for c in color), 3, cv2.LINE_AA)
        current = trail.current(now)
        if current:
            p = point(current.x, current.y)
            cv2.circle(preview, p, 7, color, -1, cv2.LINE_AA)
            text(preview, slot+' TIP', (max(5, min(p[0]+12, vw-100)), max(25, p[1]-12)), .5, color, 2)
    canvas[vy:vy+vh, vx:vx+vw] = preview
    text(canvas, 'A: teal | B: purple | index fingertip trajectories | normalized image units', (28, 620), .47, MUTED)
    state = bow.snapshot()
    text(canvas, state['state'], (920, 130), .8, TEAL if state['direction'] else ORANGE, 2)
    text(canvas, state['reason'], (920, 161), .39, WHITE)
    text(canvas, 'INPUT ENABLED' if state['enabled'] else 'PAUSED - ENTER TO ENABLE', (920, 192), .43, MUTED)
    text(canvas, 'Measured hand speed (image units/s)', (920, 237), .43, WHITE)
    for i, slot in enumerate(('A', 'B')):
        m = state['measured'].get(slot)
        label = (f"{slot}: {m['speed']:.3f} | vy {m['vy']:+.3f}" if m else slot+': awaiting valid motion')
        text(canvas, label, (920, 271+36*i), .47, TEAL if slot == 'A' else PURPLE)
    direction = 'RIGHT' if state['direction'] > 0 else 'LEFT' if state['direction'] < 0 else 'STOP'
    text(canvas, f"Bow: {direction} | {state['speed']:.3f} travel/s", (920, 359), .5, WHITE)
    text(canvas, 'Speed = mean of individual magnitudes', (920, 393), .4, MUTED)
    text(canvas, 'Both vertical directions must agree', (920, 422), .42, MUTED)
    mapping = 'UP -> LEFT | DOWN -> RIGHT' if bow.config.invert_direction else 'UP -> RIGHT | DOWN -> LEFT'
    text(canvas, mapping, (920, 466), .47, ORANGE)
    text(canvas, 'Horizontal / still / missing hand: STOP', (920, 498), .4, MUTED)
    text(canvas, f'Max speed: {bow.config.max_speed:g} travel/s', (920, 530), .44, MUTED)
    recording = info.get('recording')
    if recording:
        status = recording['state']
        label = ('READY - press C' if info.get('recording_source_ready') else 'WAITING FOR CAMERA') if status == 'READY' else status
        if status == 'COUNTDOWN':
            label += f" {math.ceil(recording['countdown_remaining_s'])}"
        if status == 'RECORDING':
            label += f" {recording['remaining_s']:.1f}s | NO AUDIO"
        text(canvas, 'Recording: '+label, (920, 570), .42, ORANGE)
        if recording.get('error') or recording.get('metadata_warning') or recording['dropped_frames'] or recording['diagnostic_drops']:
            text(canvas, 'Recording warning - inspect final status/logs', (920, 596), .36, ORANGE)
    left, right, y = 100, 1260, 685
    cv2.line(canvas, (left, y), (right, y), MUTED, 3, cv2.LINE_AA)
    for x in (left, right):
        cv2.line(canvas, (x, y-20), (x, y+20), ORANGE, 3)
    x = round(left+state['position']*(right-left))
    cv2.rectangle(canvas, (x-12, y-14), (x+12, y+14), TEAL, -1)
    if state['direction']:
        cv2.arrowedLine(canvas, (x, y-30), (x+state['direction']*50, y-30), TEAL, 3, cv2.LINE_AA)
    text(canvas, f"VIRTUAL TRAVEL {state['position']:.1%} | reverse inward to leave a limit", (100, 735), .48, WHITE)
    text(canvas, 'Space/X: stop | Enter: enable fresh motion | R: center + stop | Q: quit | C: record if enabled', (100, 773), .46, MUTED)
    return canvas
