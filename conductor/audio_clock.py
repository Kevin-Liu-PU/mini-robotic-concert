"""Device-free audio planning and bounded, sample-buffer click rendering.

No camera, sound-device import, file access or sleeping. The UI publishes immutable
plans. One audio callback consumes them. Host DAC times are estimates, not measured
acoustic onsets. This is a soft real-time Python prototype, not a timing guarantee.
"""
from dataclasses import dataclass
import math
import numpy as np


@dataclass(frozen=True)
class AudioConfig:
    sample_rate: int = 48000
    channels: int = 1
    peak_amplitude: float = .08
    click_duration_s: float = .018
    frequency_hz: float = 1000.
    requested_latency_s: float = .03
    max_output_lead_s: float = .20
    max_lateness_s: float = .020
    startup_timeout_s: float = 10.
    device: object = None
    device_name_contains: str | None = None

    def __post_init__(self):
        limits = dict(peak_amplitude=(0,.2),click_duration_s=(.005,.05),
                      frequency_hz=(100,4000),requested_latency_s=(.005,.1),
                      max_output_lead_s=(.02,.25),max_lateness_s=(0,.04),
                      startup_timeout_s=(1,30))
        if type(self.sample_rate) is not int or self.sample_rate not in (44100,48000):
            raise ValueError('Audio sample_rate must be 44100 or 48000')
        if type(self.channels) is not int or self.channels not in (1,2):
            raise ValueError('Audio channels must be 1 or 2')
        for name,(low,high) in limits.items():
            value=getattr(self,name)
            if isinstance(value,bool) or not isinstance(value,(int,float)) or not math.isfinite(value) or not low<=value<=high:
                raise ValueError(f'Invalid audio {name}: expected {low}..{high}')
        if self.requested_latency_s>self.max_output_lead_s:
            raise ValueError('Requested audio latency exceeds allowed output lead')
        if self.device is not None and not (type(self.device) is int and self.device>=0 or
                                             isinstance(self.device,str) and self.device.strip()):
            raise ValueError('Audio device must be null, a nonnegative index or a name')
        if self.device_name_contains is not None and (not isinstance(self.device_name_contains,str) or not self.device_name_contains.strip()):
            raise ValueError('device_name_contains must be null or a nonempty name')


@dataclass(frozen=True)
class AudioPlan:
    generation: int = 0
    published_at: float = 0.
    valid_until: float = 0.
    active: bool = False
    period: float = 1.
    min_spacing_s: float = .2
    # (prediction number, intended monotonic beat time); at most two candidates.
    ticks: tuple = ()
    immediate_sequence: int = 0


class AudioPlanner:
    """One-way adapter: no audio prediction can become a detected gesture."""
    def __init__(self):
        self.generation=0
        self.muted=True
        self.barrier=-math.inf
        self.latest=None
        self.immediate_sequence=0

    def toggle_mute(self, now):
        self.muted=not self.muted
        self.generation+=1
        self.latest=None
        self.immediate_sequence=0
        self.barrier=now

    def publish(self, clock, now, events=()):
        count=clock.predicted_count-sum(e['kind']=='PREDICTED_CLOCK_TICK' for e in events)
        for event in events:
            if event['kind']=='SYSTEM_FAULT' or (event['kind']=='CLOCK_CONTROL' and event.get('action') in ('STOP','ARM')):
                self.generation+=1
                self.latest=None
                self.barrier=event['time_s']
                self.immediate_sequence=(count+1
                    if event.get('reason')=='RESUME_LAST_TEMPO' else 0)
            if event['kind']=='PREDICTED_CLOCK_TICK':
                count+=1
                self.latest=(count,event['scheduled_time_s'])
        active=bool(not self.muted and clock.running and clock.healthy and clock.fault_reason is None)
        period=clock.period or 1.
        deadlines=[now+clock.cfg.heartbeat_timeout_s]
        for value in (clock.last_processed,clock.last_frame):
            if value is not None:deadlines.append(value+clock.cfg.heartbeat_timeout_s)
        ticks=[]
        if active:
            if self.latest and self.latest[1]>=self.barrier:ticks.append(self.latest)
            if clock.next_tick is not None and clock.next_tick>=self.barrier:
                ticks.append((clock.predicted_count+1,clock.next_tick))
        return AudioPlan(self.generation,now,min(deadlines),active,period,
                         clock.cfg.min_tick_spacing*period,tuple(ticks),self.immediate_sequence)


class ClickRenderer:
    """Single producer/consumer numeric ring; precomputed waveform and no I/O.

    Ring entries are never overwritten: exhausted evidence capacity stops audio.
    UI drains the ring outside the callback. CPython's single producer ownership
    and publish-last indices avoid blocking locks in the callback.
    """
    def __init__(self, config, ring_capacity=2048):
        self.cfg=config
        n=round(config.sample_rate*config.click_duration_s)
        t=np.arange(n,dtype=np.float64)/config.sample_rate
        wave=np.sin(2*np.pi*config.frequency_hz*t)*np.sin(np.pi*np.arange(n)/(n-1))**2
        self.wave=(config.peak_amplitude*wave).astype(np.float32)
        self.plan=AudioPlan()
        self.fault=None
        self.ring=np.zeros((ring_capacity,10),dtype=np.float64)
        self.read_index=self.write_index=0
        self.generation=-1
        self.seen=0
        self.last_onset=None
        self.previous_spacing=.2
        self.tail=0
        self.previous_buffer_end=None
        self.clock_offset_s=None
        self.callback_count=0
        self.driver_status_count=0
        self.last_timing=None
        self.fault_timing=None
        self.requested_count=self.skipped_count=0
        self.last_callback_at=None

    def fail(self, reason):
        if self.fault is None:
            self.fault=reason
            self.fault_timing=self.last_timing
        self.tail=0

    def _record(self, code, sequence, target, requested, estimated, pa_dac, plan):
        if self.write_index-self.read_index>=len(self.ring):
            self.fail('AUDIO_EVIDENCE_OVERFLOW')
            return False
        row=self.ring[self.write_index%len(self.ring)]
        row[0]=code;row[1]=sequence;row[2]=target;row[3]=requested
        row[4]=estimated;row[5]=pa_dac;row[6]=plan.generation
        row[7]=plan.published_at;row[8]=60/plan.period
        row[9]=int(sequence==plan.immediate_sequence)
        self.write_index+=1
        return True

    def drain_events(self):
        events=[]
        names={1:'AUDIO_PLAYBACK_REQUEST',2:'AUDIO_SKIPPED_LATE',3:'AUDIO_SKIPPED_SPACING'}
        while self.read_index<self.write_index:
            row=self.ring[self.read_index%len(self.ring)].copy()
            self.read_index+=1
            events.append(dict(kind=names[int(row[0])],prediction_number=int(row[1]),
                predicted_time_s=float(row[2]),playback_request_time_s=float(row[3]),
                host_estimated_output_time_s=float(row[4]),host_dac_time_s=float(row[5]),
                generation=int(row[6]),plan_published_time_s=float(row[7]),bpm=float(row[8]),
                immediate_resume=bool(row[9]),
                requested_alignment_error_s=float(row[4]-row[2]),acoustic_output_time_s=None))
        return events

    def render(self, output, frames, *, now, pa_current, pa_dac, status=False):
        output.fill(0)
        self.last_callback_at=now
        self.callback_count+=1
        self.last_timing=(now,pa_current,pa_dac,frames,self.previous_buffer_end,self.clock_offset_s)
        if self.fault:return
        if status:
            self.driver_status_count+=1
            self.fail('AUDIO_DRIVER_STATUS_ERROR')
            return
        if frames<=0 or output.shape!=(frames,self.cfg.channels):
            self.fail('AUDIO_BUFFER_SHAPE_ERROR')
            return
        if not all(math.isfinite(v) for v in (now,pa_current,pa_dac)) or pa_current<=0 or pa_dac<=0:
            self.fail('AUDIO_HOST_TIME_UNAVAILABLE')
            return
        lead=pa_dac-pa_current
        duration=frames/self.cfg.sample_rate
        if lead<-.002 or lead+duration>self.cfg.max_output_lead_s:
            self.fail('AUDIO_OUTPUT_LEAD_OUT_OF_BOUNDS')
            return
        # A stable clock-domain bridge is sampled before real stream startup.
        # Device-free callers can initialize it once on their first callback.
        # Never rebuild it from callback arrival jitter on every buffer.
        if self.clock_offset_s is None:self.clock_offset_s=now-pa_current
        begin=pa_dac+self.clock_offset_s
        end=begin+duration
        # Compare native host timestamps, never converted arrival timestamps.
        if self.previous_buffer_end is not None and (pa_dac<self.previous_buffer_end-.003 or pa_dac-self.previous_buffer_end>.10):
            self.fail('AUDIO_HOST_TIME_DISCONTINUITY')
            return
        self.previous_buffer_end=pa_dac+duration
        if begin-now<-.002 or end-now>self.cfg.max_output_lead_s:
            self.fail('AUDIO_CALLBACK_DEADLINE_OR_BRIDGE_ERROR')
            return
        plan=self.plan
        if plan.generation!=self.generation:
            self.generation=plan.generation
            self.seen=0
            self.tail=0
        if not plan.active:
            self.tail=0
            return
        if now>plan.valid_until:
            self.fail('AUDIO_SOURCE_HEARTBEAT_EXPIRED')
            return
        allowed=max(0,min(frames,math.floor((plan.valid_until-begin)*self.cfg.sample_rate)))
        if self.tail:
            count=min(allowed,len(self.wave)-self.tail)
            output[:count,:]=self.wave[self.tail:self.tail+count,None]
            self.tail+=count
            if self.tail>=len(self.wave) or allowed<frames:self.tail=0
        for sequence,target in plan.ticks:
            if sequence<=self.seen or target>=end:continue
            if target>plan.valid_until:continue
            sample=max(0,round((target-begin)*self.cfg.sample_rate))
            if sample>=allowed:continue
            self.seen=sequence
            estimated=begin+sample/self.cfg.sample_rate
            code=1
            # Fresh phase corrections may target "now", inside the driver's
            # committed lead. Request the earliest writable sample and report
            # that delay. Skip stale requests, not fresh requests plus DAC lead.
            if now-target>self.cfg.max_lateness_s+1e-9:code=2
            elif self.last_onset is not None and estimated-self.last_onset<min(self.previous_spacing,plan.min_spacing_s)-1/self.cfg.sample_rate:code=3
            if not self._record(code,sequence,target,now,estimated,pa_dac+sample/self.cfg.sample_rate,plan):
                output.fill(0)
                return
            if code!=1:
                self.skipped_count+=1
                continue
            count=min(allowed-sample,len(self.wave))
            output[sample:sample+count,:]=self.wave[:count,None]
            self.tail=count if count<len(self.wave) and allowed==frames else 0
            self.last_onset=estimated
            self.previous_spacing=plan.min_spacing_s
            self.requested_count+=1
