"""Synthetic geometry/gesture cases, never claims about real camera accuracy."""
import copy
import math
import unittest
from conductor_rt.body_reference import AutomaticVisualDetector, tracking_timeout, pose_side_for_hand
from conductor_rt.vision import result_record
from conductor_rt.clock import MusicalClock, BeatObservation
from conductor_rt.count_in import CountIn


def frame(wrist_y=.70, tip_y=None, dx=0., dy=0., scale=1., chin_delta=0., shape=None):
    pose=[[.5,.5,1.] for _ in range(33)]
    pose[0]=[.5,.30,1.]
    pose[11],pose[12]=[.35,.55,1.],[.65,.55,1.]
    pose[23],pose[24]=[.4,.85,1.],[.6,.85,1.]
    pose[15],pose[16]=[.35,.7,1.],[.65,wrist_y,1.]
    points=[[.65+(i%4)*.005,wrist_y-(i//4)*.006] for i in range(21)]
    points[0]=[.65,wrist_y]
    points[8]=[.65,tip_y if tip_y is not None else wrist_y-.08]
    if shape=='prepare':
        points=[[.65,wrist_y-.02] for _ in range(21)]
        points[0]=[.65,wrist_y]
        points[5],points[6],points[8]=[.65,wrist_y-.05],[.65,wrist_y-.09],[.65,wrist_y-.15]
        points[17]=[.70,wrist_y-.05]
        points[4]=[.76,wrist_y-.05]
        for i in (10,14,18):points[i]=[.66,wrist_y-.08]
    elif shape=='stop':
        points=[[.64,wrist_y-.02] for _ in range(21)]
        points[0]=[.65,wrist_y]
        points[5],points[17]=[.63,wrist_y-.02],[.68,wrist_y-.02]
        points[6],points[8],points[4]=[.64,wrist_y-.05],[.65,wrist_y-.06],[.652,wrist_y-.058]
        for i in (10,14,18):points[i]=[.64,wrist_y-.06]
        for i in (12,16,20):points[i]=[.64,wrist_y-.01]
    face={'1':[.5,.35,0.], '10':[.5,.2,0.], '33':[.42,.28,0.],
          '263':[.58,.28,0.], '152':[.5,.44+chin_delta,0.],
          '234':[.39,.34,0.], '454':[.61,.34,0.]}
    def transform(p):return [.5+(p[0]-.5)*scale+dx,.55+(p[1]-.55)*scale+dy]+p[2:]
    return dict(pose=[transform(p) for p in pose],hands=[dict(label='Right',points=[transform(p) for p in points])],
                face={k:transform(p) for k,p in face.items()})


class BodyReferenceTests(unittest.TestCase):
    def run_frames(self, records, detector=None, start=0):
        d=detector or AutomaticVisualDetector(1)
        results=[]
        for i,r in enumerate(records):
            t=start+i/30
            results.append(d.update(r,t,t+.02,t,round(t*30),90))
        return d, results

    def test_full_translation_scale_and_mild_roll_do_not_create_beats(self):
        records=[]
        for i in range(360):
            r=frame(dx=.10*math.sin(i/35),dy=.09*math.sin(i/20),scale=1+.12*math.sin(i/31))
            angle=.08*math.sin(i/22)
            def rotate(p):
                x,y=p[0]-.5,p[1]-.55
                return [.5+x*math.cos(angle)-y*math.sin(angle),.55+x*math.sin(angle)+y*math.cos(angle)]+p[2:]
            r['pose']=[rotate(p) for p in r['pose']]
            r['hands'][0]['points']=[rotate(p) for p in r['hands'][0]['points']]
            r['face']={k:rotate(p) for k,p in r['face'].items()}
            records.append(r)
        _,out=self.run_frames(records)
        self.assertGreater(sum(o['tracking_ready'] for o in out),300)
        self.assertFalse(any(o['observation'] or o['prepare'] or o['stop'] for o in out))

    def test_chin_line_motion_and_jitter_alone_never_make_a_crossing(self):
        _,out=self.run_frames([frame(wrist_y=.53,tip_y=.45,chin_delta=.025*math.sin(i/8)) for i in range(240)])
        self.assertFalse(any(o['observation'] for o in out))

    def test_rebound_survives_translation_and_scale_with_same_frame(self):
        path=[.64]*15+[.64+i*.014 for i in range(15)]+[.836-i*.012 for i in range(15)]
        _,plain=self.run_frames([frame(wrist_y=y) for y in path])
        _,moving=self.run_frames([frame(wrist_y=y,dx=.08*math.sin(i/20),dy=.04*math.sin(i/10),scale=1+.1*math.sin(i/20)) for i,y in enumerate(path)])
        events=lambda out:[(o['observation'].kind,o['observation'].frame) for o in out if o['observation']]
        self.assertTrue(events(plain));self.assertEqual(events(plain),events(moving))
        self.assertEqual(events(plain)[0][0],'wrist_rebound')

    def test_true_upstroke_crosses_auto_chin(self):
        path=[.6]*15+[.6-i*.01 for i in range(25)]
        _,out=self.run_frames([frame(wrist_y=y,tip_y=y-.08) for y in path])
        self.assertIn('chin_crossing',[o['observation'].kind for o in out if o['observation']])

    def test_occlusion_and_reacquisition_never_bridge_stale_motion(self):
        d,out=self.run_frames([frame() for _ in range(20)])
        missing=frame();missing['face']=None
        _,lost=self.run_frames([missing]*5,d,20/30)
        self.assertTrue(all(o['observation'] is None and not o['tracking_ready'] for o in lost))
        _,back=self.run_frames([frame(wrist_y=.3)]*15,d,25/30)
        self.assertTrue(all(o['observation'] is None for o in back))
        self.assertFalse(back[0]['tracking_ready']);self.assertTrue(back[-1]['tracking_ready'])

    def test_turn_clipping_and_jump_fail_closed_then_recover(self):
        for fault in ('turn','clipped','jump','missing'):
            d,_=self.run_frames([frame() for _ in range(20)])
            bad=frame()
            if fault=='turn':bad['face']['234'][2]=.2
            elif fault=='clipped':bad['face']['152'][0]=1.1
            elif fault=='jump':bad=frame(dx=.3)
            else:bad['pose']=None
            _,out=self.run_frames([bad]*12,d,20/30)
            self.assertTrue(all(o['observation'] is None for o in out))
            self.assertFalse(out[0]['tracking_ready'])

    def test_auto_prepare_and_stop_are_real_shape_candidates(self):
        _,out=self.run_frames([frame(wrist_y=.32,shape='prepare') for _ in range(30)])
        self.assertEqual(sum(bool(o['prepare']) for o in out),1)
        self.assertFalse(any(o['stop'] for o in out))
        path=[frame(wrist_y=.4)]*20+[frame(wrist_y=.4-i*.015,shape='stop') for i in range(13)]+[frame(wrist_y=.22,shape='stop')]*12
        _,out=self.run_frames(path)
        self.assertTrue(any(o['stop'] for o in out))
        self.assertFalse(any(o['prepare'] for o in out))

    def test_upper_body_without_visible_hips_is_supported(self):
        records=[]
        for _ in range(20):
            r=frame();r['pose'][23][2]=r['pose'][24][2]=0.
            records.append(r)
        _,out=self.run_frames(records)
        self.assertTrue(out[-1]['tracking_ready'])
        self.assertEqual(out[-1]['reference_reason'],'shoulders_face_hips_unavailable')

    def test_explicitly_selected_other_hand_is_supported(self):
        records=[]
        for _ in range(20):
            r=frame()
            r['pose'][16][2]=.1
            r['hands'][0]['label']='Left'
            r['hands'][0]['points']=[[x-.3,y] for x,y in r['hands'][0]['points']]
            records.append(r)
        _,out=self.run_frames(records,AutomaticVisualDetector(1,conducting_side=0))
        self.assertTrue(out[-1]['tracking_ready'])
        self.assertEqual(out[-1]['selected_side'],0)
        self.assertEqual(out[-1]['wrist_visibility']['16'],.1)

    def test_clipped_fingers_do_not_become_valid_after_relative_transform(self):
        records=[]
        for _ in range(20):
            r=frame();r['hands'][0]['points'][8][0]=1.01;records.append(r)
        _,out=self.run_frames(records)
        self.assertFalse(out[-1]['tracking_ready'])
        self.assertTrue(all(o['observation'] is None for o in out))

    def test_l_from_unselected_hand_cannot_arm_or_switch_the_fixed_hand(self):
        d=AutomaticVisualDetector(1)
        warm=frame()
        warm['pose'][15]=[.35,.7,.95]
        left=copy.deepcopy(warm['hands'][0]);left['label']='Left'
        left['points']=[[x-.3,y] for x,y in left['points']]
        warm['hands'].append(left)
        self.run_frames([warm]*20,d)
        self.assertEqual(d.selected_side,1)
        lframe=frame(wrist_y=.32,shape='prepare')
        other=copy.deepcopy(warm)
        other['pose'][15]=[.35,.32,.95]
        other['hands'][1]['points']=[[x-.3,y] for x,y in lframe['hands'][0]['points']]
        _,out=self.run_frames([other]*20,d,20/30)
        prepares=[o for o in out if o['prepare']]
        self.assertEqual(prepares,[])
        self.assertEqual(d.selected_side,1)
        self.assertTrue(all(out[-1]['prepare_hands']['0']['conditions'].values()))
        self.assertFalse(out[-1]['prepare_hands']['0']['enabled'])
        _,chosen=self.run_frames([other]*30,AutomaticVisualDetector(1,conducting_side=0))
        prepares=[o for o in chosen if o['prepare']]
        self.assertEqual(len(prepares),1)
        self.assertEqual(prepares[0]['prepare_hand'],0)
        self.assertEqual(prepares[0]['selected_side'],0)
        self.assertTrue(all(prepares[0]['prepare_hands']['0']['conditions'].values()))
        self.assertFalse(prepares[0]['prepare_hands']['1']['held_150ms'])

    def test_anatomical_choice_accounts_for_mirrored_model_input(self):
        self.assertEqual(pose_side_for_hand('right',mirrored=True),0)
        self.assertEqual(pose_side_for_hand('left',mirrored=True),1)
        self.assertEqual(pose_side_for_hand('right',mirrored=False),1)
        self.assertEqual(pose_side_for_hand('left',mirrored=False),0)

    def test_missing_selected_hand_never_borrows_the_other_visible_hand(self):
        records=[]
        for _ in range(30):
            r=frame();r['pose'][16][2]=.1
            r['hands'][0]['points']=[[x-.3,y] for x,y in r['hands'][0]['points']]
            r['hands'][0]['label']='Left';records.append(r)
        _,out=self.run_frames(records)
        self.assertFalse(any(o['tracking_ready'] or o['prepare'] or o['stop'] or o['observation'] for o in out))
        self.assertTrue(all(o['selected_side']==1 for o in out))

    def test_l_diagnostics_are_boolean_and_normal_hand_does_not_arm(self):
        _,out=self.run_frames([frame()]*30)
        status=out[-1]['prepare_hands']['1']
        self.assertTrue(status['eligible'])
        self.assertFalse(all(status['conditions'].values()))
        self.assertTrue(all(type(v) is bool for v in status['conditions'].values()))
        self.assertFalse(any(o['prepare'] for o in out))

    def test_l_locked_hand_survives_mesh_loss_without_switching_to_idle_hand(self):
        d,_=self.run_frames([frame(wrist_y=.32,shape='prepare')]*30)
        self.assertEqual(d.locked_side,1)
        d.control_state='armed'
        records=[]
        path=[.4+i*.02 for i in range(21)]+[.8-i*.016 for i in range(15)]
        for i,y in enumerate(path):
            r=frame(wrist_y=y)
            idle=copy.deepcopy(frame()['hands'][0]);idle['label']='Left'
            idle['points']=[[x-.3,v] for x,v in idle['points']]
            r['hands']=([r['hands'][0]] if not 19<=i<=23 else [])+[idle]
            records.append(r)
        _,out=self.run_frames(records,d,1.)
        self.assertTrue(all(o['selected_side']==1 for o in out))
        fallback=[o for o in out if o['association']=='pose_wrist_only']
        self.assertTrue(fallback)
        self.assertTrue(all(o['tracking_ready'] and not o['prepare'] and not o['stop'] for o in fallback))
        self.assertEqual(sum(o['observation'] is not None and o['observation'].kind=='wrist_rebound' for o in out),1)

    def test_locked_wrist_fallback_expires_and_reacquires_without_joining_motion(self):
        d,_=self.run_frames([frame(wrist_y=.32,shape='prepare')]*30)
        d.control_state='armed'
        missing=frame(wrist_y=.8);missing['hands']=[]
        _,out=self.run_frames([missing]*20,d,1.)
        self.assertFalse(out[-1]['tracking_ready'])
        self.assertEqual(out[-1]['association'],'locked_hand_unavailable')
        self.assertEqual(d.locked_side,1)
        _,back=self.run_frames([frame(wrist_y=.4)]*12,d,1+20/30)
        self.assertFalse(any(o['prepare'] or o['stop'] or o['observation'] for o in back))

    def test_sideways_corner_is_a_real_body_relative_cue(self):
        records=[]
        path=[(.65,.7)]*15+[(.65-i*.012,.7) for i in range(21)]+[(.41,.7-i*.008) for i in range(14)]
        for x,y in path:
            r=frame(wrist_y=y)
            r['pose'][16][0]=x
            r['hands'][0]['points']=[[px+x-.65,py] for px,py in r['hands'][0]['points']]
            records.append(r)
        _,out=self.run_frames(records)
        cues=[o['observation'] for o in out if o['observation']]
        self.assertEqual([o.kind for o in cues],['lateral_turn'])
        self.assertGreaterEqual(cues[0].decision_stamp-cues[0].stamp,.04)
        self.assertLess(cues[0].decision_stamp-cues[0].stamp,.3)

    def test_downward_stroke_can_turn_sideways_without_artificial_upward_bounce(self):
        path=[(.7,.5)]*15+[(.7,.5+i*.012) for i in range(21)]+[(.7-i*.008,.74) for i in range(14)]
        records=[]
        for x,y in path:
            r=frame(wrist_y=y)
            r['pose'][16][0]=x
            r['hands'][0]['points']=[[px+x-.65,py] for px,py in r['hands'][0]['points']]
            records.append(r)
        _,out=self.run_frames(records)
        cues=[o['observation'] for o in out if o['observation']]
        self.assertEqual([o.kind for o in cues],['wrist_rebound'])
        self.assertLess(cues[0].decision_stamp-cues[0].stamp,.3)

    def test_straight_sweep_and_small_tremor_do_not_create_turns(self):
        for path in ([(.8-i*.006,.64-i*.001) for i in range(60)],
                     [(.65+.012*math.sin(i),.65+.012*math.cos(i)) for i in range(120)]):
            records=[]
            for x,y in path:
                r=frame(wrist_y=y)
                r['pose'][16][0]=x
                r['hands'][0]['points']=[[px+x-.65,py] for px,py in r['hands'][0]['points']]
                records.append(r)
            _,out=self.run_frames(records)
            self.assertFalse(any(o['observation'] for o in out))

    def test_duplicate_suppression_still_allows_real_double_cadence(self):
        d=AutomaticVisualDetector(1)
        records=[frame(wrist_y=.58)]*15
        for _ in range(4):
            for i in range(15):
                fraction=i/14
                records.append(frame(wrist_y=.58+.24*(2*fraction if fraction<.5 else 2*(1-fraction))))
        cues=[]
        # Fixed hint 60 BPM; the real wrist path has 120 BPM rebounds.
        for i,record in enumerate(records):
            out=d.update(record,i/30,i/30+.02,i/30,i,60)
            if out['observation']:cues.append(out['observation'])
        self.assertEqual(len(cues),4)
        self.assertTrue(all(c.kind=='wrist_rebound' for c in cues))
        self.assertTrue(all(abs(b.stamp-a.stamp-.5)<.05 for a,b in zip(cues,cues[1:])))

    def test_reference_timeout_is_distinct_from_gesture_stop_and_keeps_short_holdover(self):
        self.assertFalse(tracking_timeout(1,'running',0,.8,2))
        self.assertTrue(tracking_timeout(3,'running',0,.8,2))
        self.assertFalse(tracking_timeout(100,'idle',None,None,2))
        c=MusicalClock(90);c.arm(0);c.start(0)
        c.tick(1);self.assertAlmostEqual(c.phase,1.5)
        self.assertEqual(c.state,'running')
        c.stop(3,'tracking_lost_safety_timeout')
        self.assertEqual(c.stopped_reason,'tracking_lost_safety_timeout')

    def test_close_camera_reference_loss_pauses_without_stop_and_needs_rearming(self):
        from conductor_rt.protocol import WireEncoder
        d,_=self.run_frames([frame() for _ in range(20)])
        near=frame(scale=2.5)
        _,out=self.run_frames([near]*70,d,20/30)
        self.assertTrue(all(not o['stop'] and o['observation'] is None for o in out))
        c=MusicalClock(90,3);c.arm(0);c.start(0)
        c.tick(1);self.assertEqual(c.state,'running')
        event=c.pause_tracking(2)[0]
        self.assertEqual(event['reason'],'tracking_lost_pause')
        frozen=c.phase;c.tick(10)
        self.assertEqual(c.phase,frozen);self.assertEqual(c.state,'paused')
        self.assertFalse(event['stop'])
        self.assertIsNone(c.stopped_reason)
        self.assertEqual(WireEncoder('test').encode(c.message(10))['state'],'ready')
        self.assertEqual(c.start(10),[])
        c.arm(11);c.start(12)
        self.assertEqual(c.phase,0);self.assertEqual(c.state,'running')

    def test_known_three_and_four_count_in_waits_for_next_real_downbeat(self):
        for meter in (3,4):
            ci=CountIn(meter)
            for i in range(meter+1):
                t=1+i*.6
                kind='chin_crossing' if i==meter-1 else 'wrist_rebound'
                o=BeatObservation(t,t+.02,frame=i,kind=kind)
                result,bpm=ci.observe(o,t+.02,0)
                if i<meter:self.assertIsNone(bpm)
            self.assertAlmostEqual(bpm,100)
            c=MusicalClock(90,meter);c.arm(0);c.initial_bpm=bpm;c.start(t+.02,'causal_count_in_downbeat',o)
            self.assertEqual(c.state,'running');self.assertEqual(c.meter,meter)


if __name__=='__main__':unittest.main()
