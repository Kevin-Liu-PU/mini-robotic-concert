"""Private all-frame comparison with shared inference and arrival-time jitter.

No camera, microphone, audio output, receiver or hardware. Stores scalar
diagnostics/events only. Clock ticks are simulated at available frames, so
these results are not live performance/publication latency measurements.
"""
import argparse
from dataclasses import asdict
import hashlib
import importlib
import importlib.util
import json
import os
from pathlib import Path
import sys
import time


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--video',type=Path,required=True)
    p.add_argument('--meter',type=int,choices=(3,4),required=True)
    p.add_argument('--hand',choices=('left','right'),required=True)
    p.add_argument('--baseline',type=Path,required=True,help='Baseline conductor directory')
    p.add_argument('--output',type=Path,required=True)
    p.add_argument('--source-offset',type=float,default=0.)
    p.add_argument('--recorded-run',type=Path,help='Original run with conductor/camera-frames.jsonl and metrics.json')
    a=p.parse_args();a.output.mkdir(parents=True,exist_ok=False)
    root=Path(__file__).resolve().parents[1]
    sys.path.insert(0,str(root/'conductor'))
    os.environ.setdefault('OMP_NUM_THREADS','4');os.environ.setdefault('OPENBLAS_NUM_THREADS','1')
    os.environ.setdefault('MPLCONFIGDIR',str(a.output/'matplotlib'))
    import cv2,mediapipe as mp
    from conductor_rt.vision import result_record
    from conductor_rt.camera_setup import prepare_frame
    from conductor_rt.body_reference import pose_side_for_hand,tracking_timeout
    spec=importlib.util.spec_from_file_location('comparison_baseline',a.baseline/'conductor_rt/__init__.py',submodule_search_locations=[str(a.baseline/'conductor_rt')])
    pkg=importlib.util.module_from_spec(spec);sys.modules[spec.name]=pkg;spec.loader.exec_module(pkg)
    timeline=None
    if a.recorded_run:
        m=json.loads((a.recorded_run/'conductor/metrics.json').read_text());ep=m['epoch_monotonic_s']
        timeline=[dict(frame=r['capture_frame'],time=r['input_s']-ep) for r in
                  map(json.loads,(a.recorded_run/'conductor/camera-frames.jsonl').read_text().splitlines())]
    hashes=lambda folder:{f.name:hashlib.sha256(f.read_bytes()).hexdigest() for f in folder.glob('*.py')}
    before=hashes(root/'conductor/conductor_rt')
    cap=cv2.VideoCapture(str(a.video))
    if not cap.isOpened():raise RuntimeError('Cannot decode local source')
    aspect=cap.get(cv2.CAP_PROP_FRAME_WIDTH)/cap.get(cv2.CAP_PROP_FRAME_HEIGHT)
    variants={}
    for name,package,jitter in [('baseline','comparison_baseline',0.),('candidate','conductor_rt',0.),
                                ('candidate_jitter_minus','conductor_rt',-.004),('candidate_jitter_plus','conductor_rt',.004)]:
        body=importlib.import_module(package+'.body_reference');clock=importlib.import_module(package+'.clock');count=importlib.import_module(package+'.count_in')
        variants[name]=dict(detector=body.AutomaticVisualDetector(aspect,conducting_side=pose_side_for_hand(a.hand),meter=a.meter),
            clock=clock.MusicalClock(90,a.meter,score_beats=None,session_id=name),count=count.CountIn(a.meter),
            jitter=jitter,last_visible=None,events=[],candidates=[],stops=[],resets=0,
            log=(a.output/(name+'-scalars.jsonl')).open('x'))
    cv2.setNumThreads(1)
    model=mp.solutions.holistic.Holistic(static_image_mode=False,model_complexity=1,smooth_landmarks=True,
        refine_face_landmarks=False,min_detection_confidence=.6,min_tracking_confidence=.6)
    def add(v,events):
        for event in events:
            v['events'].append(dict(kind=event['kind'],t=event['monotonic_ns']/1e9-100.,
                beat_index=event.get('beat_index'),bpm=event['bpm'],reason=event.get('reason')))
    frame=0;model_times=[]
    try:
        while True:
            ok,pixels=cap.read()
            if not ok:break
            if timeline:
                source=timeline[frame]['time'];frame_id=timeline[frame]['frame']
            else:source=cap.get(cv2.CAP_PROP_POS_MSEC)/1000+a.source_offset;frame_id=frame
            t0=time.perf_counter();raw=result_record(model.process(cv2.cvtColor(prepare_frame(cv2,pixels,640),cv2.COLOR_BGR2RGB)))
            model_times.append((time.perf_counter()-t0)*1000)
            for name,v in variants.items():
                stamp=100+source+v['jitter']*(1 if frame%2 else -1);now=stamp+.033
                d=v['detector'];c=v['clock'];count=v['count'];prior=d.detector;d.control_state=c.state
                out=d.update(raw,stamp,now,source,frame_id,c.bpm)
                if prior is not None and d.detector is not prior:v['resets']+=1
                if out['tracking_ready']:v['last_visible']=stamp
                if tracking_timeout(now,c.state,c.armed_at,v['last_visible'],2.):
                    add(v,c.pause_tracking(now));count.reset()
                if out['prepare']:add(v,c.arm(now,'visual_L'))
                if out['stop']:
                    v['stops'].append(dict(t=source,confirmation_s=out.get('stop_confirmation_s'),checks=out.get('stop_checks')))
                    if c.state=='running':add(v,c.stop(now,'experimental_confirmed_pinch'))
                obs=out['observation']
                if obs:
                    entry=dict(observation=asdict(obs),feature=out.get('candidate_feature'),available_s=now-100.,state=c.state)
                    if c.state=='armed':
                        entry['count_in'],bpm=count.observe(obs,now,c.armed_at)
                        if bpm is not None:
                            c.initial_bpm=bpm;add(v,c.start(now,'causal_count_in_downbeat',obs))
                    else:entry['admission']=c.observe(obs,now)
                    v['candidates'].append(entry)
                add(v,c.tick(now))
                scalar={k:out.get(k) for k in ('reference_state','association','tracking_ready','selected_side',
                    'hand_mesh_age_ms','candidate_kind','candidate_feature','beat_rejection','stop_checks','stop_approach_age_s')}
                v['log'].write(json.dumps(dict(frame=frame_id,source_s=source,**scalar))+'\n')
            frame+=1
            if frame%300==0:print('processed',frame,flush=True)
        if timeline and frame!=len(timeline):raise RuntimeError('Video/timeline length mismatch')
    finally:
        cap.release();model.close()
        for v in variants.values():v['log'].close()
    result={name:dict(events=v['events'],candidates=v['candidates'],stop_candidates=v['stops'],detector_resets=v['resets'],
        accepted=v['clock'].accepted,rejected=v['clock'].rejected,final_state=v['clock'].state,final_bpm=v['clock'].bpm,
        input_jitter_seconds=v['jitter']) for name,v in variants.items()}
    result['provenance']=dict(input=str(a.video),source_sha256=hashlib.sha256(a.video.read_bytes()).hexdigest(),frames=frame,
        meter=a.meter,hand=a.hand,source_offset=a.source_offset,original_capture_timeline=bool(timeline),
        availability_seconds=.033,camera_semantics_tracking_pause_seconds=2.,
        candidate_hashes=before,baseline_hashes=hashes(a.baseline/'conductor_rt'),
        camera_calls=0,microphone_calls=0,hardware_calls=0,landmarks_saved=False,
        warning='Shared fresh inference, simulated frame-ticked clock; not live FPS or physical/audio latency.')
    assert before==hashes(root/'conductor/conductor_rt')
    (a.output/'comparison.json').write_text(json.dumps(result,indent=2))
    print(json.dumps({name:dict(candidates=len(v['candidates']),accepted=v['accepted'],
        controls=[e for e in v['events'] if e['kind']!='beat'],stops=len(v['stop_candidates']))
        for name,v in result.items() if name!='provenance'},indent=2),flush=True)


if __name__=='__main__':main()
