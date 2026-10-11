"""Screen-only clock overlay; does not alter the baseline gesture renderer."""
import cv2
from .display import CARD, BG, WHITE, MUTED, TEAL, ORANGE, text


def audio_status_lines(state, audio, now):
    """Show both independent gates: sound enabled and a running clock."""
    muted = audio.get('muted', True)
    if audio.get('fault'):
        title = 'AUDIO FAULT - restart required'
    elif audio['state'] in ('WAITING FOR SOURCE', 'STARTING'):
        title = 'SOUND WAITING FOR SOURCE'
    elif muted:
        title = 'SOUND MUTED | M: enable'
    elif not state['armed'] or state['fault']:
        title = 'SOUND ON / CLOCK STOPPED'
    elif not state['running']:
        title = 'SOUND ON / ACQUIRING BPM'
    else:
        title = 'SOUND ON | M: mute'
    detail = (f"{audio['playback_requests']} requests | focus this window for M" if state['armed'] else
              'CLOCK STOPPED | Enter: resume/arm')
    last = audio.get('last_control')
    if last and not last['accepted'] and 0 <= now-last['time_s'] < 4:
        detail = 'M received: not ready; retry when ready' if last['reason'] != 'AUDIO_FAULT' else 'M received: audio fault; restart required'
    return title, detail


def follow_feedback(state, gesture=None):
    """Explain the current gate without presenting a prediction as a detection."""
    low, high = state.get('supported_min_bpm', 40), state.get('supported_max_bpm', 180)
    if state.get('fault'):
        return 'Output stopped by safety watchdog; recover source, then Enter.'
    if not state['armed']:
        return 'Enter: resume/arm | R: clear saved tempo | M: sound on/off'
    if not state.get('source_initialized', True):
        return 'Waiting for a fresh processed frame; no beats admitted.'
    if not state.get('tracking', True):
        return ('TRACK LOST - holding saved BPM; show the hand to rejoin.' if state['running'] else
                'TRACK LOST - show the hand; no tempo available yet.')
    feedback = state.get('tempo_feedback') or {}
    interval = feedback.get('interval_s')
    if interval and feedback.get('reason') in ('TOO_SHORT_CONTACT_REJECTED', 'OUT_OF_RANGE_INTERVAL_NEW_ANCHOR'):
        direction, limit = ('BELOW', low) if 60/interval < low else ('ABOVE', high)
        consequence = 'keeping saved BPM' if state['running'] else 'tempo not acquired'
        return f'REJECTED: {60/interval:.1f} BPM {direction} {limit:g} limit - {consequence}.'
    rejection = (gesture or {}).get('rejection') or {}
    reason = rejection.get('reason')
    if reason == 'not_armed':
        return 'CONTACT REJECTED: not prepared - lift the tip clearly above the shaded band.'
    if reason == 'refractory':
        return 'CONTACT REJECTED: too soon - repeat contact suppressed.'
    if reason == 'insufficient_downward_velocity':
        return 'CONTACT REJECTED: descent too weak - make a clear downward motion.'
    if reason == 'reacquisition_frame':
        return 'CONTACT REJECTED: reacquiring - lift and make a fresh downward contact.'
    if feedback.get('reason') == 'NEW_OBSERVED_ANCHOR':
        return 'First contact only - one more valid interval is needed to update BPM.'
    if gesture and not gesture['armed']:
        return ('PREPARING - keep the tip briefly above the shaded band.' if gesture.get('compact_pending') else
                'NOT PREPARED - lift above the shaded band before the next downward contact.')
    return 'READY - downward entry into the shaded band is one contact.'


def render_clock(canvas, state, now, recording_enabled=False, audio_state=None, gesture_state=None):
    x = 920
    cv2.rectangle(canvas, (x, 96), (1332, 575), BG, -1)
    holding = state['running'] and not state['state'].startswith('MEASURED')
    color = ORANGE if holding or state['fault'] else TEAL
    cv2.rectangle(canvas, (x, 96), (1332, 267), CARD, -1)
    saved = holding or (not state['armed'] and state['bpm'] is not None)
    text(canvas, 'LAST STABLE TEMPO' if saved else 'ESTIMATED TEMPO', (x+18, 126), .54, color)
    text(canvas, '--' if state['bpm'] is None else f"{state['bpm']:.1f}", (x+18, 190), 1.6, WHITE, 3)
    text(canvas, 'BPM', (x+297, 190), .7, MUTED)
    text(canvas, state['state'].replace('_', ' '), (x+18, 222), .48, color)
    age = state['tempo_evidence_age_s']
    detail = (f"New intervals: {state['fresh_resume_intervals']}/{state['required_resume_intervals']} | saved BPM" if state.get('resume_pending') else
              f"Evidence age: {age:.1f}s | support {state['current_evidence_confidence']:.0%}*" if age is not None else
              'Acquire: 2 contacts / 1 valid interval' if state.get('required_initial_intervals')==1 else
              f"Acquire: {state['initial_intervals']} consistent intervals")
    text(canvas, detail, (x+18, 246), .41, MUTED)
    text(canvas, '* Current support is heuristic, not accuracy', (x+18, 261), .32, MUTED)
    for top, label, stamp, count, enabled in (
        (285, 'ACTUAL DETECTED CONTACTS', state['observed_time_s'], state['observed_count'], True),
        (430, 'PREDICTED SCREEN CLOCK', state['last_predicted_time_s'], state['predicted_count'], state['running'])):
        cv2.rectangle(canvas, (x, top), (1332, top+127), CARD, -1)
        lit = enabled and stamp is not None and 0 <= now-stamp < .12
        text(canvas, label, (x+18, top+27), .48, MUTED)
        cv2.circle(canvas, (x+44, top+73), 22, (ORANGE if top == 285 else color) if lit else (73, 62, 51), -1)
        text(canvas, f"{count} detections" if top == 285 else ('RUNNING / HOLD' if holding and enabled else
             'RUNNING' if enabled else 'STOPPED' if not state['armed'] else 'WAITING'),
             (x+85, top+72), .56, WHITE)
        text(canvas, f"Range {state.get('supported_min_bpm',40):g}-{state.get('supported_max_bpm',180):g} BPM | band entry" if top == 285 else
             f"{count} predictions / {state['phase_status']}", (x+85, top+103), .35, MUTED)
    text(canvas, 'RESPONSIVE REENTRY / ACOUSTIC ALIGNMENT UNVERIFIED' if audio_state and state.get('phase_policy')=='reentry' else
         'RESPONSIVE / ACOUSTIC ALIGNMENT UNVERIFIED' if audio_state and state.get('follow_policy') else
         'AUDIO + SCREEN / ACOUSTIC ALIGNMENT UNVERIFIED' if audio_state else
         'SCREEN DEMO / PHASE ALIGNMENT IS EXPERIMENTAL', (x+2, 575), .35, ORANGE)
    cv2.rectangle(canvas, (20, 726), (906, 797), BG, -1)
    message = ('SYSTEM FAULT: '+state['fault']+' | Enter after recovery' if state['fault'] else
               ('STOPPED - Enter resumes saved BPM; R clears' if state.get('stop_reason') == 'USER_STOP' and state['bpm'] is not None else
                'STOPPED - Enter arms fresh acquisition') if not state['armed'] else
               f"Starting camera/model - first valid frame required ({state['startup_remaining_s']:.0f}s left)" if not state.get('source_initialized',True) else
               ('RESUMING saved BPM - next valid interval updates it' if state.get('follow_policy') else
                'RESUMING saved BPM - new consistent beats will update it') if state.get('resume_pending') else
               'HOLDING last BPM - keep conducting to rejoin; Space stops' if holding else
               'FOLLOWING latest valid beat interval' if state.get('follow_policy') else
               'Conduct consistent beats to acquire / guide the screen clock')
    text(canvas, message, (28, 742), .43, color)
    text(canvas, follow_feedback(state, gesture_state), (28, 762), .42, ORANGE)
    controls = ('C: REC | Space: STOP | Enter: resume/arm | R: clear | W/S: line | Q/Esc: quit'
                if recording_enabled else 'Space: STOP | Enter: resume/arm | R: clear + stop | W/S: line | Q/Esc: quit')
    text(canvas, controls,
         (28, 787), .41, WHITE)
    cv2.rectangle(canvas, (x, 762), (1340, 799), BG, -1)
    if audio_state:
        title, detail = audio_status_lines(state, audio_state, now)
        text(canvas, title, (x, 777), .46, ORANGE)
        text(canvas, detail, (x, 795), .34, MUTED)
    else:
        text(canvas, 'Screen only. No audio or robot output.', (x, 780), .43, ORANGE)
    return canvas
