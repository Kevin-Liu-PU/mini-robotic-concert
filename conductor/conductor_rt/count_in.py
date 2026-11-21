"""One-bar causal count-in for this project's known 3/4 or 4/4 convention.

Consumes an anchored downbeat and a full bar of observed pulses, then starts
on the next rebound. The last pulse may be a wrist endpoint or a finger
crossing; its existence is never inferred from elapsed time. A recognized
last-beat crossing may supply a new boundary after a shape mismatch. Ambiguous
intervals retain arming and require a visibly still hand or fresh L/Prepare
boundary. A random rebound cannot silently become a replacement first beat.
"""
import math
import statistics
from dataclasses import dataclass

@dataclass(frozen=True)
class PreparationPlan:
    cues: tuple
    period_s: float
    start_s: float
    available_s: float
    boundary_s: float
    generation: int


class CountIn:
    def __init__(self, meter=4, prepare_beats_only=False):
        if meter not in (3,4): raise ValueError("count-in meter must be 3 or 4")
        self.prepare_beats_only=prepare_beats_only
        self.meter = meter
        self.reset()

    def reset(self):
        self.cues=[]
        self.armed_at=None
        self.failed=False
        self.last_reason='waiting_first_downbeat'
        self.resets=0
        self.recovering=False
        self.recovery_since=None
        self.boundary=None
        self.missing_since=None
        self.last_context_stamp=None
        self.generation=0
        self.plan=None
        self.confirmation=None
        self.last_context_ready=False

    def sync(self, armed_at):
        if armed_at != self.armed_at:
            generation=self.generation
            self.reset();self.armed_at=armed_at;self.boundary=armed_at
            self.generation=generation+1

    def restart(self, now, armed_at, reason='explicit_prepare'):
        self.sync(armed_at)
        self.cues.clear();self.plan=None;self.confirmation=None;self.failed=False;self.recovering=False
        self.recovery_since=None;self.boundary=now;self.missing_since=None
        self.last_reason=reason;self.generation+=1
        return dict(action='count_in_reset',reason=reason,processed=now,count=0)

    def recover(self, now, reason):
        self.cues.clear();self.plan=None;self.confirmation=None;self.failed=False
        if not self.recovering:
            self.recovering=True;self.recovery_since=now;self.resets+=1
            self.generation+=1
        self.last_reason=reason
        return dict(action='count_in_reset',reason=reason,processed=now,count=0,
                    requires_boundary=True,resets=self.resets)

    def tick(self, now, armed_at):
        self.sync(armed_at)
        # 30 BPM permits two seconds between observations. The additional
        # 400 ms is delivery grace only; observe() still gates evidence time.
        if self.plan is None and self.cues and now-self.cues[-1].stamp > 2.4:
            return self.recover(now,'preparation_gap')
        return None

    def context(self, stamp, now, armed_at, tracking_ready, quiet_since=None):
        self.sync(armed_at)
        if (stamp > now+.001 or now-stamp > .4 or
                self.last_context_stamp is not None and stamp<=self.last_context_stamp):
            return None
        self.last_context_stamp=stamp
        self.last_context_ready=bool(tracking_ready)
        if not tracking_ready:
            if self.plan is not None:return self.recover(now,'scheduled_start_tracking_lost')
            if self.missing_since is None:self.missing_since=stamp
            if self.cues and stamp-self.missing_since>=.25:
                return self.recover(now,'preparation_tracking_lost')
        else:
            self.missing_since=None
            # Quiet is verified by fresh matched-hand frames, not polling,
            # missing detections, elapsed wall time, or metronome stimuli.
            if (self.recovering and quiet_since is not None and
                    stamp-max(quiet_since,self.recovery_since)>=.6):
                return self.restart(now,armed_at,'ready_after_stillness')
        return self.tick(now,armed_at)

    @property
    def stage(self):
        return 'recovering' if self.recovering else 'scheduled' if self.plan is not None else 'counting' if self.cues else 'waiting'

    def require_reprepare(self, record, reason, intervals, bpm):
        recovery=self.recover(record['processed'],reason.replace('_reprepare','_retry'))
        return dict(record,**{k:v for k,v in recovery.items() if k!='action'},
                    accepted=False,intervals_s=intervals,
                    interval_spread=max(intervals)/min(intervals),tempo_candidate_bpm=bpm),None

    def observe(self,obs,now,armed_at):
        self.sync(armed_at)
        record=dict(action='count_in',source_s=obs.source_s,frame=obs.frame,stamp=obs.stamp,
                    processed=now,count=len(self.cues),accepted=False,tempo_bpm=None)
        if self.recovering:
            return dict(record,reason='waiting_fresh_boundary',requires_boundary=True),None
        if not all(math.isfinite(x) for x in (obs.stamp,obs.received,obs.confidence)) or not .65<=obs.confidence<=1:
            return dict(record,reason='invalid_or_low_quality'),None
        if obs.origin!='vision' or obs.predicted or obs.frame<0:
            return dict(record,reason='not_real_vision'),None
        if armed_at is None or obs.stamp<=max(armed_at,self.boundary if self.boundary is not None else armed_at) or obs.stamp>now+.001 or now-obs.stamp>.4 or obs.received<obs.stamp or obs.received>now+.001:
            return dict(record,reason='stale_or_before_prepare'),None
        if self.cues and obs.stamp<=self.cues[-1].stamp:
            return dict(record,reason='duplicate_or_out_of_order'),None
        if self.cues and obs.stamp-self.cues[-1].stamp > 2.+1e-9:
            recovery=self.recover(now,'preparation_gap')
            return dict(record,reason=recovery['reason'],count=0,requires_boundary=True),None
        if self.plan is not None:
            plan=self.plan
            if obs.kind=='chin_crossing':
                return dict(record,reason='scheduled_auxiliary_crossing_ignored'),None
            # An extra early pulse contradicts the planned boundary. A fresh
            # downbeat near it may confirm later, but never changes its time.
            if obs.stamp < plan.start_s-.15*plan.period_s:
                self.recover(now,'scheduled_start_extra_pulse')
                return dict(record,count=0,reason=self.last_reason,requires_boundary=True),None
            if obs.kind=='wrist_rebound':self.confirmation=obs
            return dict(record,reason='awaiting_scheduled_boundary'),None
        position=len(self.cues)
        # An ordinary return can cross the chin line during any beat.
        # Outside the last preparation beat it is auxiliary motion, not
        # evidence that a valid partial bar must be discarded.
        if obs.kind == 'chin_crossing' and position not in (self.meter-1,self.meter):
            self.last_reason='nonfinal_chin_crossing_ignored'
            return dict(record,reason=self.last_reason),None
        if obs.kind == 'chin_crossing' and position == self.meter:
            # A raised hand can cross the line again within the previous
            # stroke. Compare with the observed preparation cadence, never the
            # initial BPM hint. This does not advance the bar or invent a cue.
            # At the final preparation position a crossing remains admissible;
            # prematurely labelling it auxiliary can itself miss a real beat.
            history=[b.stamp-a.stamp for a,b in zip(self.cues,self.cues[1:])]
            if history and obs.stamp-self.cues[-1].stamp < .5*statistics.median(history):
                self.last_reason='within_stroke_chin_crossing_ignored'
                return dict(record,reason=self.last_reason),None
        allowed=({'wrist_rebound'} if position in (0,self.meter) else
                 {'chin_crossing','wrist_rebound','lateral_turn'} if position==self.meter-1 else
                 {'wrist_rebound','lateral_turn'})
        if obs.kind not in allowed:
            if not self.cues:
                return dict(record,reason='waiting_first_downbeat'),None
            recovery=self.recover(now,'incomplete_bar_retry')
            return dict(record,reason=recovery['reason'],count=0,requires_boundary=True),None

        self.cues.append(obs)
        record.update(accepted=True,count=len(self.cues),reason='count_in_only')
        self.last_reason=record['reason']
        target=self.meter if self.prepare_beats_only else self.meter+1
        if len(self.cues)<target:return record,None
        intervals=[b.stamp-a.stamp for a,b in zip(self.cues,self.cues[1:])]
        period=sum(intervals)/len(intervals)
        median=statistics.median(intervals)
        bpm=60/period if period>0 else float('nan')
        if 30.-1e-8<=bpm<=180.+1e-8:bpm=min(180.,max(30.,bpm))
        # Reject half/double pulse mixtures. A missing cue is not a slower
        # beat and an extra rebound is not acceleration. Rapid changes may
        # require another complete bar; the running clock still follows tempo.
        # A complete bar with an interval at least twice another has an
        # unresolved beat-count/tempo interpretation. The median gates alone
        # allow, for example, 1.5/1.0/0.7 s after a missed pulse. Do not guess a
        # slower bar or rotate its boundary to the current generic rebound.
        if max(intervals)/min(intervals) >= 2.-1e-9:
            return self.require_reprepare(record,'ambiguous_bar_reprepare',intervals,bpm)
        # A single markedly short interval, immediately sandwiched by larger
        # intervals, admits both a real rapid tempo reversal and an extra
        # within-stroke candidate. Neither is an established bar. Abstain;
        # do not silently choose a phase or infer which cue was spurious.
        if any(left > 1.4*middle and right > 1.4*middle
               and self.cues[i+1].kind == 'lateral_turn'
               and self.cues[i+2].kind == 'wrist_rebound'
               for i,(left,middle,right) in enumerate(zip(intervals,intervals[1:],intervals[2:]))):
            return self.require_reprepare(record,'ambiguous_reversal_reprepare',intervals,bpm)
        if not 30<=bpm<=180 or any(not .55*median<=d<=1.6*median for d in intervals):
            return self.require_reprepare(record,'incoherent_bar_reprepare',intervals,bpm)
        if self.prepare_beats_only:
            self.plan=PreparationPlan(tuple(self.cues),period,self.cues[-1].stamp+period,
                now,max(armed_at,self.boundary),self.generation)
            self.last_reason='preparation_complete_next_beat_predicted'
            return dict(record,reason=self.last_reason,tempo_bpm=bpm,intervals_s=intervals,
                preparation_cues=self.meter,observed_intervals=len(intervals),
                scheduled_start_s=self.plan.start_s,start_origin='predicted_count_in',
                last_preparation_source_s=self.cues[-1].source_s),bpm
        record.update(reason='completed_past_bar',tempo_bpm=bpm,intervals_s=intervals,
                      first_count_source_s=self.cues[0].source_s,score_starts_at_source_s=obs.source_s,
                      source=f'{self.meter+1} already observed cues; {self.meter} elapsed quarter-beat intervals')
        self.cues.clear()
        self.last_reason=record['reason']
        return record,bpm
