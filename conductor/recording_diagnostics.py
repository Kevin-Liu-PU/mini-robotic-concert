"""Opt-in coordinate/state evidence only; no detector decision or gate changes."""
import math
from .continuity import displacement_limit


def finite(value):
    return value if isinstance(value,(int,float)) and math.isfinite(value) else None


def point(value):
    if value is None:return None
    return dict(t=getattr(value,'t',None),x=finite(getattr(value,'x',None)),y=finite(getattr(value,'y',None)))


def detector_state(engine):
    return dict(raw=point(engine.raw),smoothed=point(engine.smoothed),armed=engine.armed,
        acquisition_ready=engine.acquisition_ready,tip_continuity_history=list(engine.tip_history),
        acquired_at=engine.acquired_at,return_start_y=engine.return_start_y,
        previous_beat=engine.previous_beat,last_input=engine.last_input,
        status=engine.status,valid_intervals=list(engine.intervals),bpm=engine.bpm,
        sample_dts=list(engine.sample_dts),
        event_watermark=engine.diagnostic_sequence,configuration=vars(engine.config))


def diagnostic_record(session,hands,sequence,timestamp,origin,before,accepted=True):
    after=detector_state(session.engine)
    events=[dict(e) for e in session.engine.diagnostic_events if e['seq']>before['event_watermark']]
    candidates=[]
    for hand in hands:
        tip=hand.landmarks[8] if hand.landmarks is not None and len(hand.landmarks)>8 else None
        candidates.append(dict(wrist=dict(x=finite(hand.x),y=finite(hand.y)),
            tip8=point(tip),tip_valid=hand.index_tip is not None,label=hand.label))
    motion={}
    raw,old=after['raw'],before['raw']
    if raw and old and raw['t']>old['t']:
        dt=raw['t']-old['t']
        motion.update(observed_tip_step=math.hypot(raw['x']-old['x'],raw['y']-old['y']),
            step_dt_s=dt,step_limit=displacement_limit(session.engine.config.max_position_jump,dt,before['sample_dts']))
    smooth,prior=after['smoothed'],before['smoothed']
    if smooth and prior and smooth['t']>prior['t']:
        motion['filtered_vertical_velocity']=(smooth['y']-prior['y'])/(smooth['t']-prior['t'])
    if raw:
        motion.update(acquisition_age_s=timestamp-after['acquired_at'],
            upward_excursion_from_return_start=after['return_start_y']-raw['y'],
            raw_above_rearm_height=raw['y']<=session.engine.config.arm_y)
    motion['state_derivation_caveat']='Before/after scalar differences; inspect reset events and MAIN generation before treating them as one continuous motion'
    return dict(sequence=sequence,capture_monotonic_s=timestamp,sample_time_s=timestamp-origin,
        accepted=accepted,candidates=candidates,primary_token=session.primary_token,
        association=session.tracker.last_decision if accepted else None,
        routing=session.details(),detector_before=before,detector_after=after,
        motion=motion,events=events,coordinate_scope='Observed wrist0 and tip8, normalized detector image; mirror setting is in manifest',
        event_time_note='An association reset event can carry the previous core input time; capture_monotonic_s identifies this observation')
