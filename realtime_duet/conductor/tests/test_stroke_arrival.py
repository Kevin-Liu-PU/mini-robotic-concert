"""Causal endpoint evidence across frame rates and known meters; no devices."""
import unittest
from conductor_rt.body_reference import AutomaticVisualDetector
from conductor_rt.clock import MusicalClock
from conductor_rt.count_in import CountIn
from test_body_reference import frame


def at(x,y,side=1,shape=None):
    record=frame(wrist_y=y,shape=shape)
    record['pose'][16]=[x,y,1.]
    record['hands'][0]['points']=[[px+x-.65,py] for px,py in record['hands'][0]['points']]
    if side==0:
        record['pose'][15]=[1-x,y,1.]
        record['pose'][16]=[.65,.7,1.]
        record['hands'][0]['label']='Left'
        record['hands'][0]['points']=[[1-px,py] for px,py in record['hands'][0]['points']]
    return record


class StrokeArrivalTests(unittest.TestCase):
    def test_downstroke_arrival_is_confirmed_causally_without_waiting_for_departure(self):
        for fps in (15,30,60):
            d=AutomaticVisualDetector(1)
            observations=[]
            for i in range(round(1.5*fps)):
                t=i/fps
                y=.52+.24*min(1,max(0,(t-.5)/.4))
                out=d.update(at(.65,y),t,t+.02,t,i,60)
                if out['observation']:observations.append((out['observation'],out['candidate_feature']))
            self.assertEqual(len(observations),1,(fps,observations))
            obs,feature=observations[0]
            self.assertEqual(feature,'settled_endpoint')
            self.assertEqual(obs.kind,'wrist_rebound')
            self.assertGreaterEqual(obs.decision_stamp-obs.stamp,.06-1e-8)
            self.assertGreaterEqual(obs.decision_stamp,.94)
            self.assertLess(obs.decision_stamp,1.15)

    def test_constant_slow_sweep_and_insufficient_stroke_do_not_become_arrivals(self):
        for path in ([.5+i*.0015 for i in range(240)],
                     [.55]*15+[.55+.05*min(1,i/10) for i in range(60)]):
            d=AutomaticVisualDetector(1)
            observations=[]
            for i,y in enumerate(path):
                out=d.update(at(.65,y),i/30,i/30+.02,i/30,i,60)
                if out['observation']:observations.append(out['observation'])
            self.assertEqual(observations,[])

    def test_no_endpoint_can_bridge_an_occlusion(self):
        d=AutomaticVisualDetector(1)
        for i in range(30):
            r=at(.65,.5+.25*max(0,(i-15)/14))
            d.update(r,i/30,i/30+.02,i/30,i,60)
        missing=at(.65,.75);missing['face']=None
        for i in range(30,36):d.update(missing,i/30,i/30+.02,i/30,i,60)
        for i in range(36,65):
            out=d.update(at(.65,.75),i/30,i/30+.02,i/30,i,60)
            self.assertIsNone(out['observation'])

    def test_three_and_four_beat_arrivals_complete_count_in_on_either_fixed_hand(self):
        for meter in (3,4):
            for side in (0,1):
                with self.subTest(meter=meter,side=side):
                    d=AutomaticVisualDetector(1,conducting_side=side,meter=meter)
                    c=MusicalClock(90,meter,score_beats=None)
                    count=CountIn(meter)
                    records=[at(.65,.32,side,'prepare') for _ in range(15)]
                    positions=[(.65,.74),(.4,.74)]+([(.75,.74)] if meter==4 else [])+[(.65,.32)]
                    previous=(.65,.32)
                    for endpoint in positions+[positions[0]]:
                        for j in range(30):
                            amount=min(1,j/12)
                            x,y=[a+amount*(b-a) for a,b in zip(previous,endpoint)]
                            records.append(at(x,y,side))
                        previous=endpoint
                    starts=[];cues=[]
                    for i,r in enumerate(records):
                        t=i/30;now=t+.02;d.control_state=c.state
                        out=d.update(r,t,now,t,i,c.bpm)
                        if out['prepare']:c.arm(now,'visual_L')
                        if out['observation']:
                            obs=out['observation'];cues.append(obs)
                            if c.state=='armed':
                                _,bpm=count.observe(obs,now,c.armed_at)
                                if bpm is not None:
                                    c.initial_bpm=bpm
                                    starts+=c.start(now,'causal_count_in_downbeat',obs)
                        c.tick(now)
                    self.assertEqual(sum(e['kind']=='start' for e in starts),1,[o.kind for o in cues])
                    self.assertEqual(len(cues),meter+1)
                    self.assertAlmostEqual(c.bpm,60,delta=2)


if __name__=='__main__':unittest.main()
