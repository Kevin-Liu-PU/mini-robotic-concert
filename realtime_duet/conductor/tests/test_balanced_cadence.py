"""Independent uneven four-beat patterns, not video-specific timings."""
import unittest
from conductor_rt.clock import MusicalClock, BeatObservation
from conductor_rt.protocol import WireEncoder

def exercise(pattern,missing_fourth=False,initial=90):
    c=MusicalClock(initial,score_beats=None)
    c.arm(-.2)
    events=c.start(0,observation=BeatObservation(0,0,frame=0))
    truth=[0.]
    for i in range(1,49):truth.append(truth[-1]+pattern[(i-1)%4])
    todo=[(t+(.035 if i%4==3 else .12),i,t) for i,t in enumerate(truth[1:],1) if not(missing_fourth and i%4==3)]
    j=0;rates=[]
    for step in range(1,int((truth[-1]+.3)/.002)+1):
        now=step*.002
        while j<len(todo) and todo[j][0]<=now:
            received,i,t=todo[j]
            c.observe(BeatObservation(t,received,.85,kind='chin_crossing' if i%4==3 else 'wrist_rebound',frame=i),now)
            j+=1
        events+=c.tick(now)
        rates.append(c.bpm+60*c.correction)
    return c,truth,events,rates

class BalancedCadenceTests(unittest.TestCase):
    def test_uneven_bar_does_not_select_only_short_early_intervals(self):
        for pattern in ((.65,.70,.85,.80),(.90,.95,1.10,1.05)):
            with self.subTest(pattern=pattern):
                c,truth,events,rates=exercise(pattern)
                self.assertLess(abs(c.bpm-240/sum(pattern)),2)
                down=[e for e in events if e['kind']=='beat' and 12<=e['beat_index']<48 and e['beat_index']%4==0]
                self.assertTrue(all(abs(e['monotonic_ns']/1e9-truth[e['beat_index']])<.2 for e in down))
                self.assertGreaterEqual(min(rates),30)
                self.assertLessEqual(max(rates),180)

    def test_missing_upstroke_is_predicted_not_added_to_detected_count(self):
        c,truth,events,_=exercise((.65,.70,.85,.80),missing_fourth=True)
        self.assertEqual(c.accepted,36)
        ids=[e['beat_index'] for e in events if e['kind']=='beat']
        self.assertEqual(ids,sorted(set(ids)))
        self.assertLess(abs(c.bpm-80),2)

    def test_large_clock_gap_cannot_produce_note_burst(self):
        c=MusicalClock(60);c.arm(0);c.start(0)
        self.assertLessEqual(len(c.tick(7.01)),1)
        self.assertEqual(c.skipped_deadlines,6)

if __name__=='__main__':unittest.main()
