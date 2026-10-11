"""Quarter-note contact convention: deterministic observations, no camera/model."""
import math
import random
import unittest
from types import SimpleNamespace

from conductor.app import adjust_line_for_key
from conductor.core import Conductor, DetectorConfig, TempoMetronome
from conductor.multitrack import ConductorSession
from conductor.tracking import Hand as ObservedHand


def Hand(x,y,label,score):
    """Explicit synthetic observations with collocated valid tip/wrist landmarks."""
    return ObservedHand(x,y,label,score,[SimpleNamespace(x=x,y=y) for _ in range(21)])


def height(t, bpm=60, line=.62, excursion=.16, peak_offset=0, phase=0):
    return line+peak_offset-excursion/2+excursion/2*math.cos(2*math.pi*(t*bpm/60+phase))


def run(engine, start=0, end=16, bpm=60, fps=30, **kwargs):
    beats=[]
    for i in range(round(start*fps),round(end*fps)):
        event=engine.update(i/fps,.25,height(i/fps,bpm,**kwargs))
        if event:
            beats.append(event)
    return beats


def session_run(session, start, end, bpm=60, fps=30, dual=False, line=.62):
    events=[]
    for i in range(round(start*fps),round(end*fps)):
        t=i/fps
        hands=[Hand(.25,height(t,bpm,line),"Right",.99)]
        if dual:
            hands.append(Hand(.75,height(t,120,line,phase=.5),"Left",.99))
        event=session.update(hands,t)
        if event:
            events.append(event)
    return events


class BeatLineTests(unittest.TestCase):
    def test_touch_and_rebound_at_60_120_with_different_sizes_and_frame_rates(self):
        for bpm in (60,120):
            for fps in (20,24,30,60):
                for excursion in (.08,.12,.28,.40):
                    with self.subTest(bpm=bpm,fps=fps,excursion=excursion):
                        engine=Conductor()
                        beats=run(engine,bpm=bpm,fps=fps,excursion=excursion,phase=.13)
                        # Maximum observed y is the line, never below it.
                        self.assertGreaterEqual(len(beats),int(16*bpm/60)-1)
                        self.assertLessEqual(len(beats),int(16*bpm/60))
                        self.assertAlmostEqual(engine.bpm,bpm,delta=1)
                        self.assertTrue(all(b.t-a.t>=engine.config.refractory_s for a,b in zip(beats,beats[1:])))

    def test_just_near_line_touch_and_real_crossing_all_work(self):
        for peak_offset in (-.009,0,.04):
            engine=Conductor()
            beats=run(engine,peak_offset=peak_offset)
            self.assertEqual(len(beats),16)
            self.assertAlmostEqual(engine.bpm,60,delta=.3)

    def test_outside_tolerance_is_not_a_touch(self):
        engine=Conductor()
        self.assertEqual(run(engine,peak_offset=-.025),[])
        self.assertIsNone(engine.bpm)

    def test_contact_timestamp_is_measured_band_entry_not_predicted_bottom(self):
        engine=Conductor()
        events=run(engine,fps=120,excursion=.16)
        advance=math.acos(1-2*engine.config.line_tolerance/.16)/(2*math.pi)
        for beat in events:
            expected=round(beat.t)+0.0-advance
            self.assertAlmostEqual(beat.t,expected,delta=.001)
        self.assertTrue(all(0<round(beat.t)-beat.t<.1 for beat in events))

    def test_stationary_jitter_at_line_cannot_arm_or_repeat(self):
        for seed in (3,9):
            engine=Conductor()
            run(engine,end=4)
            count=engine.count
            rng=random.Random(seed)
            for i in range(4*60,14*60):
                engine.update(i/60,.25,.62+rng.uniform(-.02,.02))
            self.assertEqual(engine.count,count)
            self.assertIsNone(engine.bpm)

    def test_rebounds_too_small_to_rearm_cannot_double_count(self):
        engine=Conductor()
        run(engine,end=4)
        count=engine.count
        # Repeated fast bounces at the line, but never the 6% lift to .56.
        for i in range(4*120,8*120):
            engine.update(i/120,.25,.605+.022*math.cos(2*math.pi*i/12))
        self.assertEqual(engine.count,count)

    def test_jitter_irregular_time_duplicates_and_touch_at_120(self):
        rng=random.Random(205)
        engine=Conductor()
        t=0
        while t<18:
            engine.update(t,.25,height(t,120,excursion=.16)+rng.uniform(-.004,.004))
            before=(engine.count,engine.previous_beat,engine.armed)
            engine.update(t,.25,.9)
            engine.update(t-.01,None,None)
            self.assertEqual(before,(engine.count,engine.previous_beat,engine.armed))
            t+=rng.uniform(.8,1.2)/24
        self.assertIn(engine.count,(35,36))
        self.assertAlmostEqual(engine.bpm,120,delta=2)

    def test_touch_tempo_changes_without_special_gesture(self):
        engine=Conductor()
        run(engine,end=8,bpm=60)
        self.assertAlmostEqual(engine.bpm,60,delta=.3)
        run(engine,8,16,bpm=120)
        self.assertAlmostEqual(engine.bpm,120,delta=.3)
        run(engine,16,28,bpm=60)
        self.assertAlmostEqual(engine.bpm,60,delta=.3)

    def test_pause_at_line_then_resume_needs_fresh_intervals(self):
        engine=Conductor()
        run(engine,end=8)
        count=engine.count
        for i in range(240,360):
            self.assertIsNone(engine.update(i/30,.25,.62))
        self.assertIsNone(engine.output_bpm)
        self.assertEqual(engine.count,count)
        beats=run(engine,12,20)
        self.assertIsNone(beats[0].interval)
        self.assertAlmostEqual(engine.bpm,60,delta=.3)

    def test_config_rejects_overlapping_or_invalid_bands(self):
        for kwargs in ({'line_tolerance':0},{'line_tolerance':.06},
                       {'rearm_distance':.02},{'min_return':.06},
                       {'beat_y':.05},{'beat_y':.999},{'beat_y':float('nan')}):
            with self.subTest(kwargs=kwargs),self.assertRaises(ValueError):
                DetectorConfig(**kwargs)


class BeatLineSessionTests(unittest.TestCase):
    def test_line_adjustment_preserves_tracks_but_clears_every_tempo_anchor(self):
        s=ConductorSession()
        session_run(s,0,8,dual=True)
        s.select_primary('A')
        session_run(s,8,16,dual=True)
        self.assertAlmostEqual(s.engine.bpm,60,delta=.3)
        tokens={k:v.token for k,v in s.tracker.tracks.items()}
        histories={k:list(v.segments) for k,v in s.trajectories.items()}
        primary=s.primary_token
        metro=TempoMetronome()
        metro.update(15.9,s.engine.output_bpm)
        self.assertTrue(s.set_beat_line(.58,16))
        metro.update(16,s.engine.output_bpm)
        self.assertFalse(metro.running)
        self.assertEqual(s.primary_token,primary)
        self.assertEqual({k:v.token for k,v in s.tracker.tracks.items()},tokens)
        self.assertEqual({k:list(v.segments) for k,v in s.trajectories.items()},histories)
        self.assertEqual(s.engine.count,0)
        self.assertEqual(list(s.engine.intervals),[])
        self.assertIsNone(s.engine.previous_beat)
        self.assertIsNone(s.engine.last_event)
        self.assertIsNone(s.engine.output_bpm)
        self.assertFalse(s.engine.armed)
        # Late pre-adjustment frame must not seed new motion/history.
        self.assertIsNone(s.update([Hand(.25,.7,'Right',1)],15.99))
        self.assertIsNone(s.engine.raw)
        for i in range(481,510):
            self.assertIsNone(s.update([Hand(.25,.59,'Right',1),Hand(.75,.59,'Left',1)],i/30))
        beats=session_run(s,17,25,line=.58,dual=True)
        self.assertIsNone(beats[0].interval)
        self.assertAlmostEqual(s.engine.bpm,60,delta=.3)

    def test_w_s_adjust_in_opposite_directions_and_clamp_without_extra_reset(self):
        s=ConductorSession()
        self.assertTrue(adjust_line_for_key(s,ord('w'),0))
        self.assertAlmostEqual(s.engine.config.beat_y,.61)
        self.assertAlmostEqual(s.engine.config.arm_y,.55)
        self.assertTrue(adjust_line_for_key(s,ord('S'),.1))
        self.assertAlmostEqual(s.engine.config.beat_y,.62)
        s.set_beat_line(.1,.2)
        before=s.engine.diagnostic_counts['reset:beat_line_adjusted']
        self.assertFalse(adjust_line_for_key(s,ord('w'),.3))
        self.assertEqual(s.engine.diagnostic_counts['reset:beat_line_adjusted'],before)
        s.set_beat_line(.9,.4)
        self.assertFalse(adjust_line_for_key(s,ord('s'),.5))
        self.assertFalse(adjust_line_for_key(s,ord('x'),.6))

    def test_invalid_adjustment_does_not_mutate_current_measurement(self):
        s=ConductorSession()
        session_run(s,0,8)
        before=(s.engine.config,s.engine.count,s.engine.bpm,s.last_input)
        for height_value,stamp in ((.99,8),(.55,1),(.55,float('nan'))):
            with self.assertRaises(ValueError):
                s.set_beat_line(height_value,stamp)
            self.assertEqual(before,(s.engine.config,s.engine.count,s.engine.bpm,s.last_input))

    def test_adjusted_heights_keep_touch_and_rebound_behavior(self):
        for line in (.25,.50,.80):
            s=ConductorSession()
            s.set_beat_line(line,0)
            events=session_run(s,1/30,12,line=line)
            self.assertGreaterEqual(len(events),10)
            self.assertAlmostEqual(s.engine.bpm,60,delta=.3)

    def test_main_switch_resets_and_only_new_main_touches_count(self):
        s=ConductorSession()
        session_run(s,0,1)
        session_run(s,1,10,dual=True)
        self.assertEqual(s.primary,'A')
        self.assertAlmostEqual(s.engine.bpm,60,delta=.3)
        self.assertTrue(s.select_primary('B'))
        self.assertEqual(s.engine.count,0)
        self.assertIsNone(s.engine.bpm)
        events=session_run(s,10,18,dual=True)
        self.assertIsNone(events[0].interval)
        self.assertAlmostEqual(s.engine.bpm,120,delta=.3)

    def test_one_frame_loss_cannot_bridge_a_touch_or_old_interval(self):
        s=ConductorSession()
        session_run(s,0,8)
        self.assertIsNotNone(s.engine.bpm)
        self.assertIsNone(s.update([],8))
        self.assertEqual(s.engine.count,0)
        self.assertIsNone(s.engine.output_bpm)
        for i in range(241,271):
            self.assertIsNone(s.update([Hand(.25,.62,'Right',1)],i/30))
        self.assertEqual(s.engine.count,0)
        events=session_run(s,271/30,18)
        self.assertIsNone(events[0].interval)
        self.assertAlmostEqual(s.engine.bpm,60,delta=.3)

    def test_duplicate_touching_hands_do_not_double_main_tempo(self):
        solo,dual=ConductorSession(),ConductorSession()
        session_run(solo,0,1)
        session_run(dual,0,1)
        a=session_run(solo,1,12)
        b=session_run(dual,1,12,dual=True)
        self.assertEqual([x.t for x in a],[x.t for x in b])
        self.assertAlmostEqual(dual.engine.bpm,60,delta=.3)


if __name__=='__main__':
    unittest.main()
