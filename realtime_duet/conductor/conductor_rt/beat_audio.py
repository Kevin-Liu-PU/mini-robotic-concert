"""Optional, output-only synthesized clicks from published clock events.

No microphone, network, camera, or per-click subprocess. The audio callback
copies precomputed PCM; it does no file I/O, device calls, or blocking waits.
DAC times are PortAudio estimates, not microphone/physical-onset measurements.
"""
from collections import deque
import json
import math
import time


class ClickRenderer:
    """Bounded single-producer/single-consumer mailbox (CPython deque ops)."""
    def __init__(self, samplerate=48000, amplitude=.12, queue_capacity=4, stale_s=.10):
        import numpy as np
        self.samplerate = float(samplerate)
        if not 8000 <= self.samplerate <= 192000 or not 0 < amplitude <= .2:
            raise ValueError('invalid click sample rate or amplitude')
        self.queue_capacity = queue_capacity
        self.stale_s = stale_s
        self.pending = deque()
        self.records = deque(maxlen=4096)
        self.controls = deque(maxlen=4096)
        t = np.arange(round(.025*self.samplerate), dtype=np.float32)/self.samplerate
        envelope = np.minimum(t/.001, 1)*np.exp(-t/.005)
        self.waves = tuple((amplitude*gain*envelope*np.sin(2*math.pi*frequency*t)).astype('float32')[:, None]
                           for frequency, gain in ((1000, .72), (1600, 1.)))
        self.first_nonzero = 1
        self.running = False
        self.generation = 0
        self.last_sequence = -1
        self.session = None
        self.current = None
        self.position = 0
        self.duplicates = self.overflow = self.stale = self.cancelled = 0
        self.callback_count = self.underflows = self.callback_status_count = 0
        self.callback_max_ms = 0.
        self.block_min = None
        self.block_max = 0
        self.error = None

    def cancel(self):
        self.running = False
        self.generation += 1

    def submit_published(self, event):
        """Called only after the event journal is written, off the clock thread."""
        session, sequence = event['session_id'], event['seq']
        if self.session is not None and session != self.session:
            self.cancel()
            self.error = 'unexpected session change'
            return
        self.session = session
        if sequence <= self.last_sequence:
            self.duplicates += 1
            return
        self.last_sequence = sequence
        kind = event['kind']
        if kind == 'start':
            self.generation += 1
            self.running = True
        elif event['state'] != 'running':
            self.cancel()
        if kind != 'beat':
            self.controls.append(dict(kind=kind, seq=sequence,
                event_ns=event['monotonic_ns'], published_ns=event['publisher_monotonic_ns'],
                state=event['state'], generation=self.generation))
            return
        item = dict(seq=sequence, session_id=session, beat_index=event['beat_index'],
            meter=event['meter'], accented=event['beat_index'] % event['meter'] == 0,
            event_ns=event['monotonic_ns'], published_ns=event['publisher_monotonic_ns'],
            enqueued_ns=time.perf_counter_ns(), generation=self.generation,
            status='queued', callback_ns=None, estimated_dac_ns=None,
            callback_current_time=None, output_buffer_dac_time=None,
            first_sample_index=None, rendered_samples=0)
        self.records.append(item)
        if not self.running:
            item['status'] = 'inactive'
            return
        if len(self.pending) >= self.queue_capacity:
            item['status'] = 'queue_full'
            self.overflow += 1
            return
        self.pending.append(item)

    def render(self, outdata, frames, callback_ns, dac_ns=None, current_time=None, dac_time=None):
        """Copy a click once, never infer beats from BPM or replay a backlog."""
        outdata.fill(0)
        self.callback_count += 1
        self.block_min = frames if self.block_min is None else min(self.block_min, frames)
        self.block_max = max(self.block_max, frames)
        if self.current is not None and (not self.running or self.current['generation'] != self.generation):
            self.current['status'] = 'cancelled_after_render'
            self.cancelled += 1
            self.current = None
        newest = None
        # Bound callback work even when a faulty producer floods the mailbox.
        for _ in range(self.queue_capacity):
            try:
                item = self.pending.popleft()
            except IndexError:
                break
            if not self.running or item['generation'] != self.generation:
                item['status'] = 'cancelled_before_render'
                self.cancelled += 1
            elif (callback_ns-item['published_ns'])/1e9 > self.stale_s:
                item['status'] = 'stale'
                self.stale += 1
            else:
                if newest is not None:
                    newest['status'] = 'backlog_superseded'
                    self.cancelled += 1
                newest = item
        if newest is not None:
            if self.current is not None:
                self.current['status'] = 'overlap_cancelled'
                self.cancelled += 1
            self.current, self.position = newest, 0
            newest['status'] = 'rendered'
            newest['callback_ns'] = callback_ns
            newest['estimated_dac_ns'] = None if dac_ns is None else dac_ns+round(self.first_nonzero/self.samplerate*1e9)
            newest['callback_current_time'] = current_time
            newest['output_buffer_dac_time'] = dac_time
            newest['first_sample_index'] = self.first_nonzero
        if self.current is not None:
            wave = self.waves[int(self.current['accented'])]
            count = min(frames, len(wave)-self.position)
            outdata[:count] = wave[self.position:self.position+count]
            self.position += count
            self.current['rendered_samples'] += count
            if self.position == len(wave):
                self.current['status'] = 'completed'
                self.current = None


class BeatAudio(ClickRenderer):
    def __init__(self, folder):
        super().__init__()
        self.folder = folder
        self.stream = None
        self.device = None
        self.offset_s = None
        self.calibration_bracket_ms = None
        self.output_latency_s = None
        self.cpu_load_at_close = None
        self.active = False
        self.closed = False

    def start(self):
        import sounddevice as sd
        info = sd.query_devices(kind='output')
        self.device = info['name']
        self.stream = sd.OutputStream(samplerate=self.samplerate, channels=1,
            dtype='float32', latency='low', blocksize=0, callback=self.callback)
        try:
            samples = []
            for _ in range(9):
                before = time.perf_counter()
                portaudio_time = self.stream.time
                after = time.perf_counter()
                samples.append((after-before, (before+after)/2-portaudio_time))
            bracket, self.offset_s = min(samples)
            self.calibration_bracket_ms = bracket*1000
            self.output_latency_s = self.stream.latency
            self.stream.start()
            self.active = True
        except Exception:
            self.stream.close()
            raise

    def callback(self, outdata, frames, times, flags):
        begin = time.perf_counter_ns()
        try:
            if flags:
                self.callback_status_count += 1
                if flags.output_underflow:
                    self.underflows += 1
            dac_time = float(times.outputBufferDacTime)
            current_time = float(times.currentTime)
            dac_ns = round((dac_time+self.offset_s)*1e9) if self.offset_s is not None and dac_time > 0 else None
            self.render(outdata, frames, begin, dac_ns, current_time, dac_time)
        except Exception as exc:
            outdata.fill(0)
            self.error = repr(exc)
            self.cancel()
        self.callback_max_ms = max(self.callback_max_ms, (time.perf_counter_ns()-begin)/1e6)

    def status(self):
        return dict(enabled=True, active=self.active and not self.closed and self.error is None,
            device=self.device, output_only=True, microphone=False,
            rendered=sum(r['callback_ns'] is not None for r in self.records),
            underflows=self.underflows, dropped=self.overflow+self.stale+self.cancelled,
            error=self.error)

    def close(self):
        if self.closed:
            return
        self.cancel()
        if self.stream is not None:
            self.cpu_load_at_close = self.stream.cpu_load
            self.stream.abort()
            self.stream.close()
        self.active, self.closed = False, True
        for item in self.pending:
            item['status'] = 'cancelled_on_close'
        if self.current is not None:
            self.current['status'] = 'cancelled_on_close'
        with (self.folder/'audio-events.jsonl').open('x') as log:
            for item in self.records:
                log.write(json.dumps(item, allow_nan=False)+'\n')
        report = dict(self.status(), samplerate=self.samplerate, amplitude_peak_limit=.12,
            output_latency_s=self.output_latency_s, clock_offset_s=self.offset_s,
            clock_calibration_bracket_ms=self.calibration_bracket_ms,
            cpu_load_at_close=self.cpu_load_at_close, callback_count=self.callback_count,
            callback_max_ms=self.callback_max_ms, callback_status_count=self.callback_status_count,
            block_frames_min=self.block_min, block_frames_max=self.block_max,
            duplicate_events=self.duplicates, queue_overflow=self.overflow, stale_events=self.stale,
            queue_capacity=self.queue_capacity, stale_limit_s=self.stale_s, controls=list(self.controls),
            timing_claim='DAC timestamps are host estimates mapped from PortAudio time to perf_counter; no acoustic measurement or microphone.')
        (self.folder/'audio-metrics.json').write_text(json.dumps(report, indent=2, allow_nan=False))
