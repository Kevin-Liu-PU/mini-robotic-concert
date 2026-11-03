"""Read-only 2 Hz terminal view in the LAUNCHER, never the musical clock."""
import json
import math
from pathlib import Path
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
