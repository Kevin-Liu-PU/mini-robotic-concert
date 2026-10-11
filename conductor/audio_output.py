"""Opt-in output-only PortAudio lifecycle; hardware calls stay off the UI thread."""
from dataclasses import asdict
from threading import Event, Thread
import time

from .audio_clock import AudioPlanner, ClickRenderer


def sample_clock_bridge(stream, timer):
    """Bracket read-only host-clock queries; no callback/API mixing or acoustics."""
    import math
    samples=[]
    for _ in range(5):
        before=timer();host=stream.time;after=timer()
        if not all(math.isfinite(v) for v in (before,host,after)) or host<=0 or after<before:
            raise RuntimeError('Invalid stream/application clock sample')
        samples.append((after-before,(before+after)/2-host))
    elapsed,offset=min(samples)
    if elapsed>.004:raise RuntimeError('Unable to obtain a bounded stream clock bridge')
    return dict(offset_s=offset,sampling_uncertainty_s=elapsed/2,samples=len(samples),
                method='shortest bracketed stream.time query before start',acoustic_calibration=False)


def open_output_stream(config, callback):
    # Never import or initialize this backend on ordinary screen-only launches.
    import sounddevice as sd
    verified=None
    if config.device_name_contains:
        info=sd.query_devices(config.device,'output')
        if config.device_name_contains.casefold() not in info['name'].casefold():
            raise RuntimeError('Selected output no longer matches the approved device name; no stream opened')
        verified=dict(name=info['name'],host_api=sd.query_hostapis(info['hostapi'])['name'])
    stream=sd.OutputStream(device=config.device,samplerate=config.sample_rate,
        channels=config.channels,dtype='float32',blocksize=0,
        latency=config.requested_latency_s,callback=callback)
    stream.conductor_verified_output=verified
    return stream


class AudioOutput:
    def __init__(self, config, *, stream_factory=open_output_stream, timer=time.perf_counter):
        self.cfg=config
        self.timer=timer
        self.planner=AudioPlanner()
        self.renderer=ClickRenderer(config)
        self.stream_factory=stream_factory
        self.started_at=timer()
        self.ready=False
        self.error=None
        self.closed_cleanly=False
        self.driver_info=None
        self.clock_bridge=None
        self.done=Event()
        self.thread=Thread(target=self._run,daemon=True,name='conductor-audio-output')
        self.thread.start()

    def _callback(self, output, frames, info, status):
        now=self.timer()
        try:
            self.renderer.render(output,frames,now=now,
                pa_current=info.currentTime,pa_dac=info.outputBufferDacTime,status=bool(status))
        except Exception as exc:
            output.fill(0)
            self.error=f'{type(exc).__name__}: {exc}'
            self.renderer.fail('AUDIO_CALLBACK_ERROR')

    def _run(self):
        stream=None
        try:
            stream=self.stream_factory(self.cfg,self._callback)
            if self.done.is_set():return
            self.clock_bridge=sample_clock_bridge(stream,self.timer)
            self.renderer.clock_offset_s=self.clock_bridge['offset_s']
            stream.start()
            active_since=self.timer()
            self.driver_info=dict(device=stream.device,sample_rate=stream.samplerate,
                                  reported_output_latency_s=stream.latency)
            self.driver_info.update(getattr(stream,'conductor_verified_output',None) or {})
            if abs(stream.samplerate-self.cfg.sample_rate)>.01:
                raise RuntimeError('Driver sample rate differs from requested rate')
            self.ready=True
            while not self.done.wait(.02):
                if self.renderer.fault:break
                if not stream.active:
                    self.renderer.fail('AUDIO_STREAM_INACTIVE')
                    break
                last=self.renderer.last_callback_at
                if self.timer()-(last if last is not None else active_since)>.5:
                    self.renderer.fail('AUDIO_CALLBACK_HEARTBEAT_TIMEOUT')
                    break
        except Exception as exc:
            self.error=f'{type(exc).__name__}: {exc}'
            self.renderer.fail('AUDIO_OUTPUT_OPEN_OR_RUNTIME_ERROR')
        finally:
            self.ready=False
            if stream is not None:
                try:
                    try:
                        stream.abort()
                    finally:
                        stream.close()
                    self.closed_cleanly=True
                except Exception as exc:
                    self.error=f'{type(exc).__name__}: {exc}'
                    self.renderer.fail('AUDIO_CLOSE_FAILED')
            else:self.closed_cleanly=True

    @property
    def fault(self):
        if not self.ready and not self.done.is_set() and self.timer()-self.started_at>self.cfg.startup_timeout_s:
            self.renderer.fail('AUDIO_STARTUP_TIMEOUT')
            self.done.set()
        return self.renderer.fault

    def sync(self, clock, now, events=()):
        self.renderer.plan=self.planner.publish(clock,now,events)

    def toggle_mute(self, now):
        if self.ready and self.fault is None:
            self.planner.toggle_mute(now)
            return True
        return False

    def drain_events(self):
        return self.renderer.drain_events()

    def snapshot(self):
        fault=self.fault
        return dict(enabled=True,state='FAULT / RESTART REQUIRED' if fault else
                    'CLOSED' if self.done.is_set() else 'STARTING' if not self.ready else
                    'MUTED' if self.planner.muted else 'ON',muted=self.planner.muted,
                    ready=self.ready,fault=fault,error=self.error,config=asdict(self.cfg),
                    driver=self.driver_info,playback_requests=self.renderer.requested_count,
                    clock_bridge=self.clock_bridge,callback_count=self.renderer.callback_count,
                    driver_status_callbacks=self.renderer.driver_status_count,
                    last_callback_timing=self.renderer.last_timing,fault_callback_timing=self.renderer.fault_timing,
                    callback_timing_fields=['callback_perf_s','host_current_s','host_dac_s','frames',
                                            'previous_host_buffer_end_s','host_to_perf_offset_s'],
                    skipped_predictions=self.renderer.skipped_count,closed_cleanly=self.closed_cleanly,
                    acoustic_timing_verified=False,microphone_opened=False)

    def close(self):
        self.renderer.plan=type(self.renderer.plan)()
        self.done.set()
        self.thread.join(timeout=1.)
        return not self.thread.is_alive() and self.closed_cleanly
