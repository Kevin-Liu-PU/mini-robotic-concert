"""Pure observation adapters and matching helpers; no recordings are bundled."""
from types import SimpleNamespace
from conductor.tracking import Hand
from conductor.audio_clock import AudioPlan

def hands(candidates):
    result=[]
    for c in candidates:
        if 'landmarks' in c:
            p=[SimpleNamespace(**v) for v in c['landmarks']]
            x,y=p[0].x,p[0].y
        else:
            x,y=c['wrist']['x'],c['wrist']['y']
            p=[None]*9
            p[8]=SimpleNamespace(**{k:(v if v is not None else float('nan'))
                for k,v in c['tip8'].items() if k in ('x','y')})
        result.append(Hand(x if x is not None else float('nan'),y if y is not None else float('nan'),
            c['label'],c.get('label_score',0),p))
    return result

def map_video_observations(source,mapping):
    """Join through recorded video indices, including capture/queue gaps."""
    observations={r['sequence']:r for r in source};targets={}
    for entry in mapping:
        row=observations.get(entry['sequence'])
        if row is None:continue
        if row['capture_monotonic_s']!=entry['capture_monotonic_s']:
            raise ValueError('Video/diagnostic capture timestamp mismatch')
        index=entry['video_frame_index']
        if index in targets:raise ValueError('Duplicate video index')
        targets[index]=row
    return targets

def matched(reference,observed,tolerance=.15):
    """One-to-one nearby contacts; distinct model runs need not share timestamps."""
    unused=set(range(len(observed)));pairs=[];missing=[]
    for value in reference:
        choices=[i for i in unused if abs(observed[i]-value)<=tolerance]
        if not choices:missing.append(value);continue
        i=min(choices,key=lambda i:abs(observed[i]-value));unused.remove(i)
        pairs.append(dict(reference=value,observed=observed[i],difference_s=observed[i]-value))
    return dict(tolerance_s=tolerance,retained=len(pairs),pairs=pairs,missing=missing,
                additional=[observed[i] for i in sorted(unused)])

def single_click_plan(now):
    return AudioPlan(1,now,now+.5,True,1.,.6,((1,now+.1),))
