"""Independent output-only metronome. All native audio lives in a child process.

Journal times describe software scheduling/dispatch, never acoustic onset.
"""
import argparse
import json
import math
import os
from pathlib import Path
import signal
import subprocess
import sys
import threading
import time
import uuid


def validate(meter, bpm):
    if isinstance(meter, bool) or meter not in (2, 3, 4):
        raise ValueError('meter must be 2, 3 or 4')
    if isinstance(bpm, bool) or not math.isfinite(float(bpm)) or not 20 <= float(bpm) <= 240:
        raise ValueError('BPM must be between 20 and 240')
    return int(meter), float(bpm)


def atomic_status(folder, status):
    temporary = folder / 'status.tmp'
    temporary.write_text(json.dumps(status, allow_nan=False), encoding='utf-8')
    os.replace(temporary, folder / 'status.json')


def due_beat(now_ns, origin_ns, period_ns, next_index):
    """Return at most one cue, skipping missed slots instead of replaying them."""
    if now_ns < origin_ns + next_index * period_ns:
        return None
    return max(next_index, (now_ns - origin_ns) // period_ns)


class MetronomeController:
    def __init__(self, output: Path, python=sys.executable):
        self.output = Path(output)
        self.python = python
        self.child = None
        self.folder = None
        self._stop_at = None
        self._terminate_at = None
        self._status = dict(state='stopped', expected_beat=None, bar=None,
                            meter=3, bpm=70., error=None, device=None)

    def start(self, meter=3, bpm=70):
        meter, bpm = validate(meter, bpm)
        self.poll()
        if self.child is not None:
            raise RuntimeError('metronome is already active')
        self._stop_at = self._terminate_at = None
        self._status = dict(state='starting', expected_beat=None, bar=None,
                            meter=meter, bpm=bpm, error=None, device=None)
        try:
            self.folder = self.output / ('metronome-' + uuid.uuid4().hex)
            self.folder.mkdir(parents=True, exist_ok=False)
            self._status['output'] = str(self.folder)
            atomic_status(self.folder, self._status)
            with (self.folder / 'worker.log').open('xb') as log:
                self.child = subprocess.Popen(
                    [self.python, '-m', 'conductor_rt.metronome', '--output',
                     str(self.folder.resolve()), '--meter', str(meter), '--bpm', str(bpm)],
                    cwd=str(Path(__file__).resolve().parent.parent), stdin=subprocess.PIPE,
                    stdout=log, stderr=subprocess.STDOUT)
        except Exception as exc:
            self._status.update(state='error', error=str(exc))
            raise
        return dict(self._status)

    def request_stop(self):
        if self.child is None or self._stop_at is not None:
            return
        self._stop_at = time.monotonic()
        self._status['state'] = 'stopping'
        # No writes or waits: EOF requests cancellation even during native init.
        if self.child.stdin is not None:
            self.child.stdin.close()

    def poll(self):
        if self.child is None:
            return dict(self._status)
        try:
            data = json.loads((self.folder / 'status.json').read_text())
            self._status.update(data)
        except (OSError, ValueError):
            pass
        code = self.child.poll()
        if code is not None:
            if self.child.stdin is not None and not self.child.stdin.closed:
                self.child.stdin.close()
            self._status['child_exit'] = code
            if self._stop_at is not None and not self._status.get('error'):
                self._status['state'] = 'stopped'
            elif code != 0 or self._status['state'] not in ('stopped', 'error'):
                self._status.update(state='error', error=self._status.get('error') or
                                    'Metronome worker exited unexpectedly (%s)' % code)
            self.child = None
        elif self._stop_at is not None:
            self._status['state'] = 'stopping'
            now = time.monotonic()
            try:
                if self._terminate_at is None and now - self._stop_at >= .75:
                    self.child.terminate()
                    self._terminate_at = now
                elif self._terminate_at is not None and now - self._terminate_at >= .5:
                    self.child.kill()
            except ProcessLookupError:
                pass
        return dict(self._status)

    def close(self):
        self.request_stop()
        deadline = time.monotonic() + 1.75
        while self.child is not None and time.monotonic() < deadline:
            self.poll()
            if self.child is not None:
                time.sleep(.01)
        if self.child is not None:
            self.child.kill()
            try:
                self.child.wait(timeout=.25)
            except subprocess.TimeoutExpired:
                pass
            self.poll()


def watch_stdin(stop, stream):
    """Any command or EOF is cancellation; no detector/control input exists."""
    try:
        stream.read(1)
    finally:
        stop.set()


def run_worker(folder, meter, bpm, stop=None, audio_factory=None):
    meter, bpm = validate(meter, bpm)
    folder = Path(folder)
    stop = stop if stop is not None else threading.Event()
    status = dict(state='starting', expected_beat=None, bar=None, meter=meter,
                  bpm=bpm, error=None, device=None,
                  timing_claim='Software schedule/dispatch only; not acoustic or photon onset.')
    audio = None
    exit_code = 0
    try:
        atomic_status(folder, status)
        if audio_factory is None:
            from .beat_audio import BeatAudio
            audio_factory = BeatAudio
        if stop.is_set():
            return 0
        audio = audio_factory(folder)
        audio.start()
        status['device'] = audio.device
        if stop.is_set():
            return 0
        origin = time.perf_counter_ns()
        period = round(60e9 / bpm)
        session = folder.name
        sequence = 0
        with (folder / 'stimulus.jsonl').open('x', encoding='utf-8') as journal:
            def publish(kind, index, scheduled, dispatch):
                nonlocal sequence
                event = dict(session_id=session, seq=sequence, kind=kind,
                             state='running' if kind != 'stop' else 'stopped',
                             beat_index=index, meter=meter, bpm=bpm,
                             monotonic_ns=scheduled, scheduled_ns=scheduled,
                             publisher_monotonic_ns=dispatch, dispatch_ns=dispatch)
                sequence += 1
                journal.write(json.dumps(event) + '\n')
                journal.flush()
                audio.submit_published(event)
            publish('start', 0, origin, origin)
            next_index = 0
            status['state'] = 'running'
            while not stop.is_set():
                if audio.error:
                    raise RuntimeError(audio.error)
                now = time.perf_counter_ns()
                index = due_beat(now, origin, period, next_index)
                if index is None:
                    stop.wait(min(.02, max(0, (origin + next_index * period - now) / 1e9)))
                    continue
                if stop.is_set():
                    break
                scheduled = origin + index * period
                publish('beat', index, scheduled, now)
                status.update(expected_beat=index % meter + 1, bar=index // meter + 1,
                              beat_index=index, scheduled_ns=scheduled, dispatch_ns=now,
                              skipped_beats=status.get('skipped_beats', 0) + index - next_index)
                atomic_status(folder, status)
                next_index = index + 1
            publish('stop', next_index, time.perf_counter_ns(), time.perf_counter_ns())
    except Exception as exc:
        exit_code = 1
        status.update(state='error', error=str(exc))
    finally:
        if audio is not None:
            try:
                audio.cancel()
                audio.close()
            except Exception as exc:
                exit_code = 1
                status.update(state='error', error=status.get('error') or str(exc))
        if status['state'] != 'error':
            status['state'] = 'stopped'
        atomic_status(folder, status)
    return exit_code


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--meter', type=int, default=3)
    parser.add_argument('--bpm', type=float, default=70)
    args = parser.parse_args()
    stop = threading.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: stop.set())
    threading.Thread(target=watch_stdin, args=(stop, sys.stdin), daemon=True).start()
    return run_worker(args.output, args.meter, args.bpm, stop)


if __name__ == '__main__':
    raise SystemExit(main())
