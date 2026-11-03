"""One-bar causal count-in for this project's known 3/4 or 4/4 convention.

Consumes an anchored downbeat and a full bar of observed pulses, then starts
on the next rebound. The last pulse may be a wrist endpoint or a finger
crossing; its existence is never inferred from elapsed time. An incomplete bar resynchronizes while still
armed; it never invents a missing beat or requires an unannounced second L.
"""
import math
import statistics

class CountIn:
    def __init__(self, meter=4):
        if meter not in (3,4): raise ValueError("count-in meter must be 3 or 4")
        self.meter = meter
        self.reset()

    def reset(self):
        self.cues=[]
        self.armed_at=None
        self.failed=False
        self.last_reason='waiting_first_downbeat'
        self.resets=0

    def observe(self,obs,now,armed_at):
        if armed_at != self.armed_at:
            self.reset();self.armed_at=armed_at
        record=dict(action='count_in',source_s=obs.source_s,frame=obs.frame,stamp=obs.stamp,
                    processed=now,count=len(self.cues),accepted=False,tempo_bpm=None)
        if not all(math.isfinite(x) for x in (obs.stamp,obs.received,obs.confidence)) or not .65<=obs.confidence<=1:
            return dict(record,reason='invalid_or_low_quality'),None
        if obs.origin!='vision' or obs.predicted or obs.frame<0:
            return dict(record,reason='not_real_vision'),None
        if armed_at is None or obs.stamp<=armed_at or obs.stamp>now+.001 or now-obs.stamp>.4 or obs.received<obs.stamp or obs.received>now+.001:
            return dict(record,reason='stale_or_before_prepare'),None
        if self.cues and obs.stamp<=self.cues[-1].stamp:
            return dict(record,reason='duplicate_or_out_of_order'),None
        position=len(self.cues)
        allowed=({'wrist_rebound'} if position in (0,self.meter) else
                 {'chin_crossing','wrist_rebound','lateral_turn'} if position==self.meter-1 else
                 {'wrist_rebound','lateral_turn'})
        if obs.kind not in allowed:
            self.resets+=bool(self.cues)
            self.cues.clear()
            # A fresh downstroke may be the next bar's first beat. Reuse only
            # that observed cue; the lost/ambiguous interval is discarded.
            if obs.kind=='wrist_rebound':self.cues.append(obs)
            self.last_reason='incomplete_bar_resync'
            return dict(record,count=len(self.cues),accepted=bool(self.cues),
                        reason=self.last_reason,resets=self.resets),None
        self.cues.append(obs)
        record.update(accepted=True,count=len(self.cues),reason='count_in_only')
        self.last_reason=record['reason']
        if len(self.cues)<self.meter+1:return record,None
        intervals=[b.stamp-a.stamp for a,b in zip(self.cues,self.cues[1:])]
        period=sum(intervals)/self.meter
        median=statistics.median(intervals)
        bpm=60/period if period>0 else float('nan')
        # Reject half/double pulse mixtures. A missing cue is not a slower
        # beat and an extra rebound is not acceleration. Rapid changes may
        # require another complete bar; the running clock still follows tempo.
        if not 30<=bpm<=180 or any(not .55*median<=d<=1.6*median for d in intervals):
            self.cues.clear()
            self.cues.append(obs)
            self.resets+=1
            self.last_reason='incoherent_bar_resync'
            return dict(record,count=1,accepted=False,reason=self.last_reason,resets=self.resets),None
        record.update(reason='completed_past_bar',tempo_bpm=bpm,intervals_s=intervals,
                      first_count_source_s=self.cues[0].source_s,score_starts_at_source_s=obs.source_s,
                      source=f'{self.meter+1} already observed cues; {self.meter} elapsed quarter-beat intervals')
        self.cues.clear()
        self.last_reason=record['reason']
        return record,bpm
