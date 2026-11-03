"""Shared automatic chin-line Stop, without recording-specific heights."""
import copy
import math
import unittest
from test_body_reference import frame
from test_stroke_arrival import at
from test_stop_and_gaps import run


def path(side=1,fps=30,height=.15,transient=False):
    reference=frame();chin=reference['face']['152'][1]
    span=reference['pose'][12][0]-reference['pose'][11][0]
    end=chin-height*span;begin=chin+.6*span
    samples=[]
    for i in range(round(1.7*fps)):
        t=i/fps;amount=min(1,max(0,(t-.5)/.4))
        y=begin+amount*(end-begin)
        pinched=(.88<=t<1.00) if transient else t>=.5
        samples.append((t,at(.7,y,side,'stop' if pinched else None)))
    return samples


def transform(record,t):
    record=copy.deepcopy(record)
    scale=1+.12*math.sin(t*2);dx=.06*math.sin(t*2);dy=.06*math.cos(t*3)
    angle=.05*math.sin(t*3)
    def point(p):
        x,y=(p[0]-.5)*scale,(p[1]-.55)*scale
        return [.5+x*math.cos(angle)-y*math.sin(angle)+dx,
                .55+x*math.sin(angle)+y*math.cos(angle)+dy]+p[2:]
    record['pose']=[point(p) for p in record['pose']]
    record['hands'][0]['points']=[point(p) for p in record['hands'][0]['points']]
    record['face']={k:point(p) for k,p in record['face'].items()}
    return record


class SharedChinStopTests(unittest.TestCase):
    def test_above_chin_below_nose_stops_on_both_hands_rates_and_meters(self):
        for side in (0,1):
            for fps in (30,60):
                for meter in (3,4):
                    for height in (.02,.10,.20):
                        with self.subTest(side=side,fps=fps,meter=meter,height=height):
                            samples=path(side,fps,height)
                            stops=[(t,r) for (t,_),r in zip(samples,run(samples,side,meter)) if r['stop']]
                            self.assertTrue(stops)
                            self.assertGreaterEqual(stops[0][1]['stop_confirmation_s'],.22)
                            self.assertLess(stops[0][0],1.35)
                            self.assertTrue(all(stops[0][1]['stop_checks'].values()))

    def test_whole_body_translation_distance_and_roll_preserve_confirmation(self):
        for side in (0,1):
            samples=path(side);moving=[(t,transform(r,t)) for t,r in samples]
            times=lambda rows,out:[t for (t,_),r in zip(rows,out) if r['stop']]
            plain=times(samples,run(samples,side));shifted=times(moving,run(moving,side))
            self.assertTrue(shifted);self.assertAlmostEqual(plain[0],shifted[0],places=6)

    def test_below_chin_pinch_stays_rejected_with_approach_and_stationarity(self):
        for height in (-.02,-.15,-.4):
            out=run(path(height=height))
            self.assertFalse(any(r['stop'] for r in out))
            self.assertTrue(any(r.get('stop_checks',{}).get('pinch') for r in out))

    def test_transient_and_moving_pinches_do_not_stop(self):
        for side in (0,1):
            for fps in (30,60):
                self.assertFalse(any(r['stop'] for r in run(path(side,fps,transient=True),side)))
            samples=path(side)
            samples=[(t,at(.7,.35+.065*math.sin((t-.9)*2*math.pi/.35),side,'stop') if t>.9 else r) for t,r in samples]
            self.assertFalse(any(r['stop'] for r in run(samples,side)))

    def test_stationary_pinch_and_body_movement_do_not_supply_approach(self):
        samples=[(i/30,transform(at(.7,.39,shape='stop'),i/30)) for i in range(120)]
        self.assertFalse(any(r['stop'] for r in run(samples)))

    def test_occlusion_during_confirmation_cannot_bridge_old_evidence(self):
        samples=path()
        for t,r in samples:
            if .93<t<1.1:r['face']=None
        self.assertFalse(any(r['stop'] for r in run(samples)))

    def test_chin_jitter_is_not_a_replacement_for_sustained_shape(self):
        samples=path(height=-.02)
        for i,(t,r) in enumerate(samples):
            if t>.9:r['face']['152'][1]+=.03 if i%2 else -.03
        self.assertFalse(any(r['stop'] for r in run(samples)))

    def test_pose_or_face_nose_height_does_not_control_shared_chin_stop(self):
        samples=path()
        for t,r in samples:
            r['pose'][0][1]=.12;r['face']['1'][1]=.25
        self.assertTrue(any(r['stop'] for r in run(samples)))


if __name__=='__main__':unittest.main()
