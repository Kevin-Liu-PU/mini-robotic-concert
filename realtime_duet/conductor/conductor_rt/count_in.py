"""Optional one-bar causal count-in for this project's known 4/4 convention.

Consumes three rebound cues and one upward chin-crossing cue, then starts on
the next rebound. No music runs during count-in. Missing or ambiguous cues
never silently turn into an estimated tempo or start command.
"""
import math
import statistics

class CountIn:
    def __init__(self):
        self.reset()

    def reset(self):
        self.cues=[]
        self.armed_at=None
        self.failed=False

    def observe(self,obs,now,armed_at):
        record=dict(action='count_in',source_s=obs.source_s,frame=obs.frame,stamp=obs.stamp,
                    processed=now,count=len(self.cues),accepted=False,tempo_bpm=None)
        if armed_at != self.armed_at:
            self.reset();self.armed_at=armed_at
        if self.failed:return dict(record,reason='new_prepare_required'),None
        if not all(math.isfinite(x) for x in (obs.stamp,obs.received,obs.confidence)) or not .65<=obs.confidence<=1:
            return dict(record,reason='invalid_or_low_quality'),None
        if obs.origin!='vision' or obs.predicted or obs.frame<0:
            return dict(record,reason='not_real_vision'),None
        if armed_at is None or obs.stamp<=armed_at or obs.stamp>now+.001 or now-obs.stamp>.4 or obs.received<obs.stamp or obs.received>now+.001:
            return dict(record,reason='stale_or_before_prepare'),None
        if self.cues and obs.stamp<=self.cues[-1].stamp:
            return dict(record,reason='duplicate_or_out_of_order'),None
        expected=('wrist_rebound','wrist_rebound','wrist_rebound','chin_crossing','wrist_rebound')[len(self.cues)]
        if obs.kind!=expected:
            self.failed=bool(self.cues)
            self.cues.clear()
            return dict(record,reason='incomplete_count_in_prepare_and_repeat'),None
        self.cues.append(obs)
        record.update(accepted=True,count=len(self.cues),reason='count_in_only')
        if len(self.cues)<5:return record,None
        intervals=[b.stamp-a.stamp for a,b in zip(self.cues,self.cues[1:])]
        period=sum(intervals)/4
        median=statistics.median(intervals)
        bpm=60/period if period>0 else float('nan')
        if not 30<=bpm<=180 or any(not .35*median<=d<=2*median for d in intervals):
            self.failed=True
            self.cues.clear()
            return dict(record,accepted=False,reason='incoherent_count_in_prepare_and_repeat'),None
        record.update(reason='completed_past_bar',tempo_bpm=bpm,intervals_s=intervals,
                      first_count_source_s=self.cues[0].source_s,score_starts_at_source_s=obs.source_s,
                      source='five already observed cues; four elapsed quarter-beat intervals')
        self.cues.clear()
        return record,bpm
