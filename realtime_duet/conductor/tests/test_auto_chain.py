"""Synthetic perception→count-in→continuous beat chain, conductor only.

No real pixels/model/user accuracy claim; real TCP/camera are separate trials.
"""
import unittest
from conductor_rt.body_reference import AutomaticVisualDetector
from conductor_rt.clock import MusicalClock
from conductor_rt.count_in import CountIn
from test_body_reference import frame


def gesture_frames(meter, bars=4, stop=True, periods=None):
    records=[frame(wrist_y=.32,shape='prepare') for _ in range(30)]
    records += [frame(wrist_y=.58) for _ in range(10)]
    for bar in range(bars):
        n=round((periods[bar] if periods else 1.)*30)
        for beat in range(meter):
            for i in range(n):
                f=i/max(1,n-1)
                if beat==meter-1:
                    y=.58-.23*(2*f if f<.5 else 2*(1-f))
                else:
                    y=.58+.24*(2*f if f<.5 else 2*(1-f))
                records.append(frame(wrist_y=y))
    if stop:
        records += [frame(wrist_y=.58-i*.02,shape='stop') for i in range(19)]
        records += [frame(wrist_y=.22,shape='stop') for _ in range(20)]
    return records


def chain(records,meter=4):
    detector=AutomaticVisualDetector(1)
    clock=MusicalClock(90,meter,session_id='synthetic',score_beats=12*meter)
    count=CountIn(meter)
    events=[]
    candidates=[];starts=[];stops=[];accepted=[];tempos=[]
    for i,record in enumerate(records):
        stamp=i/30;now=stamp+.02
        detector.control_state=clock.state
        result=detector.update(record,stamp,now,stamp,i,clock.bpm)
        if result['tracking_ready']:clock.last_visual=stamp
        if result['prepare']:
            clock.arm(now,'visual_L')
        if result['stop'] and clock.state=='running':
            stopped=clock.stop(now,'experimental_confirmed_pinch')
            stops += stopped;events+=stopped
        obs=result['observation']
        if obs:
            candidates.append((obs.kind,obs.stamp))
            if clock.state=='armed':
                _,bpm=count.observe(obs,now,clock.armed_at)
                if bpm is not None:
                    clock.initial_bpm=bpm
                    started=clock.start(now,'causal_count_in_downbeat',obs)
                    starts += started;events+=started
            else:accepted.append(clock.observe(obs,now))
        events+=clock.tick(now)
        tempos.append(clock.bpm)
    return dict(clock=clock,events=events,candidates=candidates,starts=starts,stops=stops,accepted=accepted,tempos=tempos)


class AutomaticChainTests(unittest.TestCase):
    def test_visual_prepare_count_in_start_stop_reaches_beats_for_both_meters(self):
        for meter in (3,4):
            with self.subTest(meter=meter):
                r=chain(gesture_frames(meter),meter)
                self.assertTrue(r['starts'],r['candidates'])
                self.assertEqual(r['starts'][0]['reason'],'causal_count_in_downbeat')
                self.assertTrue(r['stops'])
                self.assertEqual(r['clock'].stopped_reason,'experimental_confirmed_pinch')
                self.assertTrue(any(e['kind']=='beat' for e in r['events']))
                self.assertEqual(r['events'][-1]['kind'],'stop')
                self.assertTrue(any(a['accepted'] for a in r['accepted']))

    def test_first_and_last_score_beats_and_new_session_restart(self):
        for meter in (3,4):
            r=chain(gesture_frames(meter,bars=15,stop=False),meter)
            beats=[e for e in r['events'] if e['kind']=='beat']
            self.assertEqual([e['beat_index'] for e in beats],list(range(12*meter)))
            self.assertEqual(r['clock'].state,'ended')
            self.assertEqual(r['events'][-1]['kind'],'end')
        r=chain(gesture_frames(4))
        self.assertTrue(r['starts']);self.assertTrue(r['stops'])

    def test_active_acceleration_and_deceleration_do_not_become_stop(self):
        r=chain(gesture_frames(4,bars=10,stop=False,periods=[1.,1.,.9,.8,.7,.7,.8,.9,1.,1.1]))
        self.assertTrue(r['starts'])
        self.assertFalse(r['stops'])
        self.assertGreater(max(r['tempos']),70)
        self.assertLess(r['tempos'][-1],max(r['tempos'])-5)

if __name__=='__main__':unittest.main()
