"""Synthetic clock scenarios; simulation timestamps, never recorded evidence."""
from bisect import bisect_left, bisect_right
from collections import Counter
import random
from statistics import mean
from conductor.screen_clock import ScreenClock, ScreenClockConfig

def simulate(beats, *, duration=None, lost=(), controls=(), faults=(), stalls=(), duplicates=(), config=None):
    controller = ScreenClock(config)
    trace, journal = [], []
    pending = sorted(beats)
    index = 0
    commands = sorted(controls)
    command_index = 0
    duration = duration if duration is not None else max(beats, default=0)+5
    for i in range(round(duration*100)+1):
        now = i/100
        if any(a <= now < b for a,b in stalls):
            continue
        while command_index < len(commands) and commands[command_index][0] <= now+1e-9:
            _,action = commands[command_index]
            if action == 'stop':
                controller.stop(now)
            elif action == 'arm':
                controller.arm(now)
            command_index += 1
        event = None
        if index < len(pending) and pending[index]+.04 <= now+1e-9:
            event = pending[index]
            index += 1
        fault = next((name for a,b,name in faults if a <= now < b), None)
        frame = None if any(a <= now < b for a,b in duplicates) else now-.02
        row = controller.step(now, frame_time=frame, track_good=not any(a <= now < b for a,b in lost),
                              beat_time=event, fault=fault)
        trace.append(row)
        journal.extend(controller.drain_events())
    return controller, trace, journal


def regular(bpm, count, start=.5):
    return [start+i*60/bpm for i in range(count)]


def case(name, periods, *, drop=(), extra=(), jitter=0, phase_shift=None, **options):
    truth = [.5]
    for p in periods:
        truth.append(truth[-1]+p)
    if phase_shift:
        index,shift = phase_shift
        truth = [t+(shift if i >= index else 0) for i,t in enumerate(truth)]
    rng = random.Random(81)
    beats = sorted([t+rng.uniform(-jitter,jitter) for i,t in enumerate(truth) if i not in drop]+list(extra))
    return dict(name=name, truth=truth, beats=beats, options=options)


def cases():
    return [
        case('SYNTHETIC_regular_60', [1]*30),
        case('SYNTHETIC_regular_120', [.5]*50),
        case('SYNTHETIC_regular_40', [1.5]*20),
        case('SYNTHETIC_regular_180', [1/3]*60),
        case('SYNTHETIC_single_miss_60', [1]*30, drop=(12,)),
        case('SYNTHETIC_single_miss_120', [.5]*50, drop=(20,)),
        case('SYNTHETIC_single_miss_40', [1.5]*25, drop=(10,)),
        case('SYNTHETIC_two_misses_60', [1]*30, drop=(12,13)),
        case('SYNTHETIC_midpoint_extra', [1]*30, extra=(11.,)),
        case('SYNTHETIC_short_extra', [1]*30, extra=(10.68,)),
        case('SYNTHETIC_jitter_60', [1]*40, jitter=.025),
        case('SYNTHETIC_jitter_120', [.5]*60, jitter=.02),
        case('SYNTHETIC_step_60_90', [1]*10+[2/3]*25),
        case('SYNTHETIC_step_60_120', [1]*10+[.5]*35),
        case('SYNTHETIC_step_120_60', [.5]*20+[1]*25),
        case('SYNTHETIC_step_120_40', [.5]*20+[1.5]*18),
        case('SYNTHETIC_ramp_60_120', [1]*8+[60/(60+3*i) for i in range(1,21)]+[.5]*12),
        case('SYNTHETIC_ramp_120_60', [.5]*12+[60/(120-3*i) for i in range(1,21)]+[1]*12),
        case('SYNTHETIC_phase_shift_350ms', [1]*30, phase_shift=(12,.35)),
        case('SYNTHETIC_phase_advance_350ms', [1]*30, phase_shift=(12,-.35)),
        case('SYNTHETIC_short_hand_loss', [1]*30, lost=((10.7,10.8),)),
        case('SYNTHETIC_long_hand_loss_return', [1]*40, drop=tuple(range(11,26)), lost=((10.7,25.7),)),
        case('SYNTHETIC_long_stationary_return', [1]*40, drop=tuple(range(11,26))),
        case('SYNTHETIC_long_pause_shifted_return', [1]*10+[15.35]+[1]*12),
        case('SYNTHETIC_visible_pause_60s', [1]*10, duration=75),
        case('SYNTHETIC_hand_exit_60s', [1]*10, duration=75, lost=((10.7,76),)),
        case('SYNTHETIC_user_stop_latched', [1]*30, controls=((10.7,'stop'),)),
        case('SYNTHETIC_user_stop_then_arm', [1]*30, controls=((10.7,'stop'),(15.7,'arm'))),
        case('SYNTHETIC_camera_fault', [1]*30, faults=((10.7,11.2,'CAMERA_WORKER_UNAVAILABLE'),)),
        case('SYNTHETIC_processing_stall', [1]*30, stalls=((10.7,11.5),)),
        case('SYNTHETIC_duplicate_source_frames', [1]*30, duplicates=((10.7,11.5),)),
        case('SYNTHETIC_fault_then_explicit_arm', [1]*30,
             faults=((10.7,11.2,'CAMERA_WORKER_UNAVAILABLE'),), controls=((12.7,'arm'),)),
    ]


def intervals(trace, predicate):
    spans = []
    active = False
    for a,b in zip(trace,trace[1:]):
        if predicate(a):
            if not active:
                spans.append([a['time_s'],b['time_s']])
            else:
                spans[-1][1] = b['time_s']
            active = True
        else:
            active = False
    return dict(total_s=sum(b-a for a,b in spans), longest_s=max((b-a for a,b in spans), default=0), spans=spans)


def metrics(item, config=None):
    controller, trace, journal = simulate(item['beats'], config=config, **item['options'])
    times = [r['time_s'] for r in trace]
    samples = []
    for i,t in enumerate(item['truth'][1:],1):
        idx = bisect_left(times,t+.05)
        if idx < len(trace):
            dt=t-item['truth'][i-1]
            samples.append(dict(t=t, index=i, bpm=trace[idx]['bpm'], target=60/dt,
                                continuous_truth=1/3-1e-9<=dt<=1.5+1e-9))
    # Long deliberate pauses have no target tempo interval. Holding during a
    # rest must not be scored as an attempt to conduct a very slow beat.
    errors = [abs(s['bpm']/s['target']-1)*100 for s in samples
              if s['index']>=5 and s['bpm'] is not None and s['continuous_truth']]
    ticks = [r for r in journal if r['kind']=='PREDICTED_CLOCK_TICK']
    actual = [r for r in journal if r['kind']=='OBSERVED_DETECTION']
    gap = [b['time_s']-a['time_s'] for a,b in zip(ticks,ticks[1:])]
    stable = [r['time_s'] for r in trace if r['running']]
    latency = None
    if 'step_' in item['name']:
        change = 10.5
        target = samples[-1]['target']
        post = [s for s in samples if s['t']>change]
        for i in range(len(post)-2):
            if all(s['bpm'] is not None and abs(s['bpm']/target-1)<=.05 for s in post[i:i+3]):
                latency = post[i]['t']-change
                break
    phase_recovery = None
    if 'phase_' in item['name']:
        start = item['truth'][12]
        phase_samples = []
        for t in item['truth'][12:]:
            idx = bisect_left(times,t+.05)
            if idx<len(trace):
                row=trace[idx];p=60/row['bpm'] if row['bpm'] else None;n=row['next_predicted_time_s']
                phase_samples.append((t,abs((n-t+p/2)%p-p/2) if p and n else 99))
        for i in range(len(phase_samples)-2):
            if all(e <= .05 for _,e in phase_samples[i:i+3]):
                phase_recovery = phase_samples[i][0]-start+.05
                break
    ramp_errors = []
    if 'ramp_' in item['name']:
        lo,hi=(9,28) if '60_120' in item['name'] else (13,32)
        ramp_errors=[abs(s['bpm']/s['target']-1)*100 for s in samples if lo<=s['index']<=hi and s['bpm'] is not None]
    phase_mae=[]
    for tick in ticks:
        t=tick['time_s'];i=bisect_right(item['truth'],t)
        if 5<=t<item['truth'][-1] and 0<i<len(item['truth']) and item['truth'][i]-item['truth'][i-1]<=1.5+1e-9:
            phase_mae.append(min(abs(t-beat) for beat in item['truth']))
    return_recovery = None
    if item['name']=='SYNTHETIC_long_pause_shifted_return':
        returning=item['truth'][11:]
        return_errors=[]
        for t in returning:
            idx=bisect_left(times,t+.05);row=trace[idx]
            p=60/row['bpm'];n=row['next_predicted_time_s']
            return_errors.append((t,abs((n-t+p/2)%p-p/2)))
        for i in range(len(return_errors)-2):
            if all(e<=.05 for _,e in return_errors[i:i+3]):
                return_recovery=return_errors[i][0]-returning[0]+.05
                break
    result = dict(name=item['name'], source='LABELED_SYNTHETIC',observed_inputs=len(item['beats']), observed_logged=len(actual),
        predicted_ticks=len(ticks), first_running_time_s=stable[0] if stable else None,
        clock_running=intervals(trace,lambda r:r['running']),
        holding=intervals(trace,lambda r:r['running'] and not r['state'].startswith('MEASURED')),
        mean_absolute_tempo_error_percent=mean(errors) if errors else None,
        tempo_error_samples=len(errors),unsupported_truth_intervals_excluded=sum(not s['continuous_truth'] for s in samples),
        phase_error_samples=len(phase_mae),error_scope='Continuous supported truth intervals only; deliberate pauses excluded.',
        unavailable_error_samples=sum(s['bpm'] is None for s in samples if s['index']>=5),
        ramp_only_mean_absolute_error_percent=mean(ramp_errors) if ramp_errors else None,
        step_response_5percent_3beats_s=latency,phase_recovery_50ms_3beats_s=phase_recovery,
        shifted_return_phase_recovery_50ms_3beats_s=return_recovery,
        predicted_nearest_truth_mae_ms=1000*mean(phase_mae) if phase_mae else None,
        min_emitted_tick_gap_s=min(gap) if gap else None,
        fault=controller.fault_reason, final_bpm=trace[-1]['bpm'],
        decisions=dict(Counter(r.get('reason',r['kind']) for r in journal)))
    return result,trace,journal
