import unittest
from conductor_rt.count_in import CountIn
from conductor_rt.clock import BeatObservation

def cue(t,index,kind='wrist_rebound',**kwargs):
    return BeatObservation(t,t+.1,frame=index,kind=kind,source_s=t,**kwargs)

class CountInTests(unittest.TestCase):
    def test_complete_seen_bar_estimates_tempo_only_on_next_downbeat(self):
        c=CountIn()
        times=(1.,1.65,2.35,3.2,4.)
        for i,t in enumerate(times):
            r,bpm=c.observe(cue(t,i,'chin_crossing' if i==3 else 'wrist_rebound'),t+.1,0)
            if i<4:self.assertIsNone(bpm)
        self.assertAlmostEqual(bpm,80)
        self.assertEqual(r['score_starts_at_source_s'],4)
        self.assertEqual(r['first_count_source_s'],1)

    def test_missing_upstroke_never_invents_a_start(self):
        c=CountIn()
        for i,t in enumerate((1.,2.,3.,5.,6.,7.,8.)):
            _,bpm=c.observe(cue(t,i),t+.1,0)
            self.assertIsNone(bpm)
        r,_=c.observe(cue(9,9),9.1,8.5)
        self.assertEqual(r['count'],1)

    def test_incomplete_bar_recovers_without_second_prepare_or_invented_cue(self):
        for meter in (3,4):
            c=CountIn(meter)
            c.observe(cue(1,1),1.1,0)
            r,bpm=c.observe(cue(2,2,'chin_crossing'),2.1,0)
            self.assertIsNone(bpm);self.assertEqual(r['reason'],'incomplete_bar_resync')
            for i in range(meter+1):
                kind='wrist_rebound' if i in (0,meter) else 'chin_crossing' if i==meter-1 else 'lateral_turn'
                r,bpm=c.observe(cue(3+i,3+i,kind),3.1+i,0)
                if i<meter:self.assertIsNone(bpm)
            self.assertAlmostEqual(bpm,60)
            self.assertEqual(r['first_count_source_s'],3)

    def test_lateral_motion_cannot_start_but_can_supply_observed_final_pulse(self):
        c=CountIn(3)
        r,bpm=c.observe(cue(1,1,'lateral_turn'),1.1,0)
        self.assertFalse(r['accepted']);self.assertIsNone(bpm)
        for i,k in enumerate(('wrist_rebound','lateral_turn','lateral_turn','wrist_rebound'),2):
            _,bpm=c.observe(cue(i,i,k),i+.1,0)
            if i<5:self.assertIsNone(bpm)
        self.assertAlmostEqual(bpm,60)

    def test_old_prediction_low_quality_and_duplicate_cannot_count(self):
        c=CountIn()
        for o in (cue(0,0),cue(1,1,predicted=True),cue(1,2,confidence=.2)):
            r,bpm=c.observe(o,1.1,.5)
            self.assertFalse(r['accepted']);self.assertIsNone(bpm)
        c.observe(cue(1,3),1.1,.5)
        r,_=c.observe(cue(1,3),1.2,.5)
        self.assertEqual(r['reason'],'duplicate_or_out_of_order')

if __name__=='__main__':unittest.main()
