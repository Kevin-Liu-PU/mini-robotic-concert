"""Read-only 2 Hz terminal view in the LAUNCHER, never the musical clock."""
import json
import math
from pathlib import Path
import re
import threading
import time

def latest_snapshot(path):
    try:
        with Path(path).open('rb') as f:
            f.seek(0,2);size=f.tell();f.seek(max(0,size-16384));data=f.read(16384)
    except OSError:return None
    if not data.endswith(b'\n'):
        data=data.rsplit(b'\n',1)[0] if b'\n' in data else b''
    for line in reversed(data.splitlines()):
        try:
            row=json.loads(line)
            if isinstance(row,dict) and row.get('version')==1 and row.get('kind') in ('CLOCK','STOP'):
                return row
        except (ValueError,UnicodeError):pass
    return None

def session_failure(output, returncode):
    """Why the sender ended badly, in plain words: dict(kind, title, advice), or None when it ended
    cleanly. Reads only what the sender left in its own session folder, so that the window can show
    the reason instead of leaving it in sender-console.log."""
    if not isinstance(returncode, int) or isinstance(returncode, bool) or returncode == 0:
        return None
    output = Path(output)
    try:
        console = (output / 'sender-console.log').read_text(encoding='utf-8', errors='replace')[-20000:]
    except OSError:
        console = ''
    try:
        status = json.loads((output / 'conductor' / 'startup-status.json').read_text(encoding='utf-8'))
    except (OSError, ValueError):
        status = {}
    if not isinstance(status, dict):
        status = {}
    text = str(status.get('error') or '') + '\n' + console
    again = ' To retry, restart both terminals, terminal 1 first.'
    if 'not authorized to capture video' in console:
        return dict(kind='camera_permission', title='Start failed: this terminal has no camera permission',
                    advice='macOS has not yet allowed this terminal program to use the camera, so the camera cannot open. '
                           'Run the camera permission check once in the same terminal and click Allow when the system asks. '
                           'The check command is printed in the terminal.' + again)
    if 'camera access has been denied' in console:
        return dict(kind='camera_denied', title='Start failed: camera permission denied',
                    advice='Open System Settings, go to Privacy & Security, then Camera, and switch on this terminal program. Then restart the terminal program.' + again)
    if 'Selected camera could not be opened' in text:
        return dict(kind='camera', title='Start failed: the camera cannot open',
                    advice='The camera may be in use by another program or not connected. Close the program that uses the camera and try again.' + again)
    if 'No consumer within --wait-client deadline' in text:
        return dict(kind='no_receiver', title='Start failed: no receiver found',
                    advice='Start the receiver in terminal 1 first, wait for its listening line, then start the Conductor. The receiver must be restarted for every run.')
    try:
        metrics = json.loads((output / 'conductor' / 'metrics.json').read_text(encoding='utf-8'))
    except (OSError, ValueError):
        metrics = {}
    recording = metrics.get('recording') if isinstance(metrics, dict) else None
    if (isinstance(recording, dict) and recording.get('error') and metrics.get('final_state') in ('ended', 'stopped')
            and not metrics.get('errors') and not metrics.get('publisher_error')):
        # The sender ends with an error code when only its own recording failed. The piece itself was
        # fine, so this case gets its own message instead of "error while running".
        return dict(kind='recording',
                    title='The piece finished normally, but the local recording failed' if metrics['final_state'] == 'ended' else 'Stopped; the local recording failed',
                    advice='Detection and playing were not affected; only the recording of this run on the Conductor side is missing. Reason: ' + str(recording['error'])[:200])
    last = next((line.strip() for line in reversed(text.splitlines())
                 if re.match(r'^[A-Za-z_][\w.]*(Error|Exception)\b.*:\s*\S', line.strip())), None)
    started = status.get('phase') == 'first_frame_received'
    return dict(kind='error', title='Error while running; the Conductor has exited' if started else 'Start failed',
                advice=(last[:300] if last else 'process exit code %d' % returncode) + '. Details are in sender-console.log in the run folder.' + again)


def status_text(snapshot,startup='explicit',now_ns=None):
    if snapshot is None:return '[MOCK] WAITING for conductor clock; no music started'
    try:
        phase=float(snapshot['beat_position']);bpm=float(snapshot['estimated_tempo_bpm']);rate=float(snapshot['tempo_bpm'])
        meter=int(snapshot['meter'][0]);stamp=int(snapshot['source_monotonic_ns'])
        if not all(math.isfinite(x) for x in (phase,bpm,rate)) or phase<0 or meter not in (3,4):raise ValueError()
    except (KeyError,ValueError,TypeError,OverflowError):return '[MOCK] INVALID status snapshot'
    now_ns=time.perf_counter_ns() if now_ns is None else now_ns
    age=(now_ns-stamp)/1e9
    freshness=' STALE' if age>1 or age<-.01 else ''
    state=snapshot.get('state','unknown').upper()
    quality=snapshot.get('quality','unreliable').upper()
    if state=='READY' and startup=='count-in':
        return f'[MOCK] READY count-in; hint={bpm:.2f} BPM (not music tempo) phase={phase:.3f} {quality}{freshness}'
    if state=='ENDED':return f'[MOCK] ENDED phase={phase:.3f} BPM={bpm:.2f} {quality}{freshness}'
    bar=math.floor(phase/meter)+1;beat=phase%meter+1
    return f'[MOCK] {state} BPM={bpm:.2f} rate={rate:.2f} bar={bar} beat={beat:.3f} phase={phase:.3f} {quality}{freshness}'

class TerminalStatus:
    def __init__(self,path,startup='explicit'):
        self.path=Path(path);self.startup=startup;self.finished=threading.Event()
        self.thread=threading.Thread(target=self.run,daemon=True,name='terminal-status-view')
    def show_once(self):
        try:print(status_text(latest_snapshot(self.path),self.startup),flush=True)
        except (OSError,ValueError):pass
    def run(self):
        while not self.finished.is_set():
            self.show_once()
            self.finished.wait(.5)
    def start(self):self.thread.start()
    def close(self):
        self.finished.set();self.thread.join(timeout=.25)
