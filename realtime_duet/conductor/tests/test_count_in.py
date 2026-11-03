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

    def test_missing_upstroke_blocks_start_until_new_prepare(self):
        c=CountIn()
        for i,t in enumerate((1.,2.,3.,5.,6.,7.,8.)):
            _,bpm=c.observe(cue(t,i),t+.1,0)
            self.assertIsNone(bpm)
        r,_=c.observe(cue(9,9),9.1,8.5)
        self.assertEqual(r['count'],1)

    def test_old_prediction_low_quality_and_duplicate_cannot_count(self):
        c=CountIn()
        for o in (cue(0,0),cue(1,1,predicted=True),cue(1,2,confidence=.2)):
            r,bpm=c.observe(o,1.1,.5)
            self.assertFalse(r['accepted']);self.assertIsNone(bpm)
        c.observe(cue(1,3),1.1,.5)
        r,_=c.observe(cue(1,3),1.2,.5)
        self.assertEqual(r['reason'],'duplicate_or_out_of_order')

if __name__=='__main__':unittest.main()
