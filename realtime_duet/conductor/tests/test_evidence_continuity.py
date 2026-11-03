"""Bounded same-wrist evidence and startup contracts; no model or devices."""
import copy
import math
import unittest
from conductor_rt.body_reference import AutomaticVisualDetector,tracking_timeout
from conductor_rt.clock import MusicalClock,BeatObservation
from conductor_rt.count_in import CountIn
from test_body_reference import frame
from test_stroke_arrival import at
from test_stop_shared_chin import path,transform
from test_stop_and_gaps import run


class ContinuityTests(unittest.TestCase):
    def test_repeated_prepare_does_not_erase_observed_count_in(self):
        for meter in (3,4):
            c=MusicalClock(90,meter);count=CountIn(meter);c.arm(0,'visual_L')
            for i in range(meter+1):
                t=1+i*.8
                self.assertEqual(c.arm(t,'visual_L'),[])
                self.assertEqual(c.armed_at,0)
                obs=BeatObservation(t,t+.02,frame=i)
                record,bpm=count.observe(obs,t+.02,c.armed_at)
                self.assertEqual(record['count'],i+1)
            self.assertAlmostEqual(bpm,75)
            c.initial_bpm=bpm
            self.assertEqual(c.start(t+.02,'causal_count_in_downbeat',obs)[0]['kind'],'start')

    def test_count_in_accepts_real_endpoints_at_different_rates_and_jitter(self):
        for meter in (3,4):
            for period in (.4,.7,1.2,1.8):
                for endpoint in ('wrist_rebound','lateral_turn','chin_crossing'):
                    for jitter in (-.004,0,.004):
                        c=CountIn(meter)
                        for i in range(meter+1):
                            t=1+i*period+(jitter if i%2 else 0)
                            kind=endpoint if i==meter-1 else 'wrist_rebound'
                            r,bpm=c.observe(BeatObservation(t,t+.1,frame=i,kind=kind),t+.1,0)
                            if i<meter:self.assertIsNone(bpm)
                        self.assertAlmostEqual(bpm,60/period,delta=1.6)

    def test_half_double_and_missing_pulses_cannot_complete_a_bar(self):
        for intervals in ((.8,.35,1.3,.8),(.8,.8,1.6,.8),(.4,1.,1.,1.)):
            c=CountIn(4);t=1.
            c.observe(BeatObservation(t,t+.05,frame=0),t+.05,0)
            for i,dt in enumerate(intervals,1):
                t+=dt;r,bpm=c.observe(BeatObservation(t,t+.05,frame=i),t+.05,0)
                self.assertIsNone(bpm)
            self.assertEqual(r['reason'],'incoherent_bar_resync')

    def test_mesh_gap_during_observed_ascent_can_confirm_current_crossing(self):
        for side in (0,1):
            d=AutomaticVisualDetector(1,conducting_side=side)
            cues=[]
            for i in range(50):
                t=i/30;y=.62-.32*min(1,max(0,(t-.5)/.6))
                r=at(.65,y,side)
                if .70<=t<=.83:r['hands']=[]
                out=d.update(r,t,t+.02,t,i,90)
                if out['observation']:cues.append(out['observation'])
            crossings=[o for o in cues if o.kind=='chin_crossing']
            self.assertEqual(len(crossings),1)
            self.assertGreaterEqual(crossings[0].stamp,.83)

    def test_stationary_wrist_and_reference_motion_do_not_supply_gap_crossing(self):
        d=AutomaticVisualDetector(1)
        for i in range(75):
            t=i/30;r=frame(wrist_y=.53,tip_y=.45,chin_delta=.02*math.sin(i/8))
            if 25<=i<=29:r['hands']=[]
            out=d.update(r,t,t+.02,t,i,90)
            self.assertIsNone(out['observation'])

    def test_mesh_gap_keeps_motion_but_never_holds_pinch_shape(self):
        for side in (0,1):
            samples=path(side,height=.15)
            for t,r in samples:
                if .73<=t<=1.03:r['hands']=[]
            out=run(samples,side)
            waiting=[(t,o) for (t,_),o in zip(samples,out) if o['association']=='locked_hand_waiting_mesh']
            self.assertTrue(waiting)
            self.assertTrue(all(not(o['stop'] or o['prepare'] or o['observation'] or o['tracking_ready']) for _,o in waiting))
            stops=[(t,o) for (t,_),o in zip(samples,out) if o['stop']]
            self.assertTrue(stops)
            first,decision=stops[0]
            self.assertGreaterEqual(decision['stop_confirmation_s'],.22)
            fresh=[o for (t,_),o in zip(samples,out) if decision['stop_evidence_stamp']<=t<=first
                   and o.get('stop_shape_valid') and o['association']=='accepted']
            self.assertGreaterEqual(len(fresh),6)

    def test_long_mesh_loss_and_low_wrist_visibility_discard_old_approach(self):
        for fault in ('long_mesh','wrist_visibility','face','other_hand'):
            samples=path()
            for t,r in samples:
                if .73<=t<=1.43:
                    if fault=='long_mesh':r['hands']=[]
                    elif fault=='wrist_visibility':r['pose'][16][2]=.1
                    elif fault=='face':r['face']=None
                    else:
                        r['hands'][0]['points']=[[x-.3,y] for x,y in r['hands'][0]['points']]
            self.assertFalse(any(o['stop'] for o in run(samples)),fault)
        self.assertTrue(tracking_timeout(5,'running',0,2.9,2))

    def test_fresh_pinch_window_is_canceled_by_new_downstroke(self):
        samples=path(height=.15)
        for i,(t,r) in enumerate(samples):
            if .97<t<1.10:samples[i]=(t,at(.7,.40+(t-.97)*.5,shape='stop'))
            elif t>=1.10:samples[i]=(t,at(.7,.43,shape='stop'))
        self.assertFalse(any(o['stop'] for o in run(samples)))

    def test_body_motion_and_short_gap_do_not_convert_low_pinch_into_stop(self):
        samples=path(height=-.15)
        moved=[]
        for t,r in samples:
            if .8<t<.96:r['hands']=[]
            if r['hands']:r=transform(r,t)
            moved.append((t,r))
        self.assertFalse(any(o['stop'] for o in run(moved)))


if __name__=='__main__':unittest.main()
