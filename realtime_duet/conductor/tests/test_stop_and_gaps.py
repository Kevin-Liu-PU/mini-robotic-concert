"""Stop intent and short compute gaps, across rates/meters; no camera or model."""
import unittest
from conductor_rt.body_reference import AutomaticVisualDetector
from test_stroke_arrival import at


def run(samples,side=1,meter=3):
    detector=AutomaticVisualDetector(1,conducting_side=side,meter=meter)
    detector.control_state='running'
    return [detector.update(record,t,t+.02,t,i,60) for i,(t,record) in enumerate(samples)]


def raised_path(fps,side=1,transient=False):
    samples=[]
    for i in range(round(1.7*fps)):
        t=i/fps;amount=min(1,max(0,(t-.5)/.4))
        x=.45+.28*amount;y=.5-.28*amount
        if t>.9:x+=.025*min(1,(t-.9)/.08)
        shape=('stop' if .88<=t<1.00 else None) if transient else ('stop' if t>=.5 else None)
        samples.append((t,at(x,y,side,shape)))
    return samples


class StopIntentTests(unittest.TestCase):
    def test_upward_diagonal_then_outward_settle_stops_on_either_hand(self):
        for side in (0,1):
            for fps in (30,60):
                with self.subTest(side=side,fps=fps):
                    samples=raised_path(fps,side)
                    output=run(samples,side)
                    stops=[(t,r) for (t,_),r in zip(samples,output) if r['stop']]
                    self.assertTrue(stops)
                    self.assertGreater(stops[0][0],1.0)
                    self.assertLess(stops[0][0],1.45)
                    self.assertGreaterEqual(stops[0][1]['stop_confirmation_s'],.22)
                    self.assertTrue(all(stops[0][1]['stop_checks'].values()))

    def test_low_frame_rate_keeps_existing_sample_count_safety_gate(self):
        # At 15 fps a 280 ms window cannot contain the required six samples.
        # Preserve that baseline limit; do not lower it to make a case pass.
        output=run(raised_path(15))
        self.assertFalse(any(r['stop'] for r in output))
        high=[r for r in output if r.get('stop_shape_valid')]
        self.assertTrue(high)
        self.assertTrue(all(not r['stop_checks']['frames'] for r in high))

    def test_transient_pinch_does_not_stop_even_after_valid_approach(self):
        for side in (0,1):
            for fps in (15,30,60):
                self.assertFalse(any(r['stop'] for r in run(raised_path(fps,side,True),side)))

    def test_stationary_high_pinch_without_approach_and_old_approach_fail(self):
        self.assertFalse(any(r['stop'] for r in run([(i/30,at(.7,.22,shape='stop')) for i in range(90)])))
        samples=raised_path(30)
        samples=[(t,at(.73,.22,shape='stop') if t>1.55 else at(.45+.28*min(1,max(0,(t-.5)/.4)),.5-.28*min(1,max(0,(t-.5)/.4)))) for t,_ in samples]
        samples.extend((i/30,at(.73,.22,shape='stop')) for i in range(51,80))
        self.assertFalse(any(r['stop'] for r in run(samples)))

    def test_downstroke_cancels_previous_stop_approach(self):
        samples=[]
        for i in range(60):
            t=i/30
            if t<.5:y=.5
            elif t<.8:y=.5-(t-.5)/.3*.32
            elif t<.95:y=.18+(t-.8)/.15*.08
            else:y=.26
            samples.append((t,at(.7,y,shape='stop' if t>=.95 else None)))
        self.assertFalse(any(r['stop'] for r in run(samples)))

    def test_normal_three_four_beat_paths_and_brief_pinches_do_not_stop(self):
        for meter in (3,4):
            for side in (0,1):
                for transient in (False,True):
                    with self.subTest(meter=meter,side=side,transient=transient):
                        samples=[(i/30,at(.65,.32,side,'prepare')) for i in range(20)]
                        previous=(.65,.32)
                        positions=[(.65,.74),(.4,.74)]+([(.75,.74)] if meter==4 else [])+[(.65,.22)]
                        for bar,period in enumerate((1.,.8,.65,.9,1.1)):
                            n=round(period*30)
                            for beat,endpoint in enumerate(positions):
                                for j in range(n):
                                    fraction=j/max(1,n-1);amount=min(1,fraction/.4)
                                    x,y=[a+amount*(b-a) for a,b in zip(previous,endpoint)]
                                    shape='stop' if transient and beat==meter-1 and .45<=fraction<.55 else None
                                    samples.append((len(samples)/30,at(x,y,side,shape)))
                                previous=endpoint
                        self.assertFalse(any(r['stop'] for r in run(samples,side,meter)))

    def test_stop_evidence_cannot_bridge_short_compute_gap(self):
        samples=raised_path(30)
        samples=[(t,r) for t,r in samples if not .90<t<1.13]
        # Resume in a held position; no fresh approach is observed afterwards.
        samples=[(t,at(.755,.22,shape='stop') if t>=1.13 else r) for t,r in samples]
        self.assertFalse(any(r['stop'] for r in run(samples)))


class ComputeGapTests(unittest.TestCase):
    def warm(self):
        return [(i/30,at(.65,.60)) for i in range(16)]

    def test_valid_short_gap_resumes_reference_but_needs_new_crossing(self):
        samples=self.warm()+[(.733333,at(.65,.55)),(.766667,at(.65,.52)),(.8,at(.65,.48)),(.833333,at(.65,.40))]
        samples += [(i/30,at(.65,.35)) for i in range(26,38)]
        output=run(samples)
        resumed=output[16]
        self.assertTrue(resumed['reference_gap_resumed'])
        self.assertTrue(resumed['tracking_ready'])
        self.assertIsNone(resumed['observation'])
        cues=[r['observation'] for r in output[17:] if r['observation']]
        self.assertEqual([c.kind for c in cues],['chin_crossing'])
        self.assertGreaterEqual(cues[0].stamp,.766667)

    def test_crossing_hidden_inside_gap_is_not_fabricated(self):
        output=run(self.warm()+[(.733333+i/30,at(.65,.3)) for i in range(15)])
        self.assertTrue(output[16]['reference_gap_resumed'])
        self.assertFalse(any(r['observation'] or r['stop'] for r in output))

    def test_long_gap_or_explicit_missing_reference_still_requires_reacquisition(self):
        long=run(self.warm()+[(.9+i/30,at(.65,.3)) for i in range(15)])
        self.assertFalse(long[16]['tracking_ready'])
        self.assertFalse(any(r['observation'] or r['stop'] for r in long))
        missing=at(.65,.6);missing['face']=None
        output=run(self.warm()+[(.533333,missing)]+[(.733333+i/30,at(.65,.3)) for i in range(15)])
        self.assertFalse(output[17]['tracking_ready'])
        self.assertFalse(output[17]['reference_gap_resumed'])
        self.assertFalse(any(r['observation'] or r['stop'] for r in output))

    def test_body_translation_and_reference_jump_after_gap_do_not_make_a_beat(self):
        from test_body_reference import frame
        samples=self.warm()+[(.733333+i/30,frame(wrist_y=.6,dx=.08,dy=.05,scale=1.08)) for i in range(15)]
        self.assertFalse(any(r['observation'] or r['stop'] for r in run(samples)))
        bad=frame(wrist_y=.6);bad['face']['234'][2]=.2
        output=run(self.warm()+[(.733333,bad)])
        self.assertFalse(output[-1]['tracking_ready'])
        self.assertFalse(output[-1]['reference_gap_resumed'])


if __name__=='__main__':unittest.main()
