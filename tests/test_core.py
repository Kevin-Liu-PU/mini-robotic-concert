import math
import random
import unittest

from conductor.core import Conductor, DetectorConfig, TempoMetronome
from conductor.tracking import Hand, HandSelector


def feed(engine, start, seconds, bpm, fps=60, jitter=0, seed=17, irregular=False, phase=0):
    rng = random.Random(seed)
    t = start
    events = []
    estimates = []
    while t < start+seconds-1e-8:
        y = 0.51+0.28*math.cos(2*math.pi*((t-start)*bpm/60+phase)) + rng.uniform(-jitter,jitter)
        event = engine.update(t,0.5,y)
        if event:
            events.append(event)
        if engine.bpm:
            estimates.append((t,engine.bpm))
        t += (1+rng.uniform(-0.30,0.30) if irregular else 1)/fps
    return events,estimates


class GestureTests(unittest.TestCase):
    def test_regular_60_and_120_at_multiple_frame_rates(self):
        for bpm in (60,120):
            for fps in (24,30,60):
                with self.subTest(bpm=bpm,fps=fps):
                    engine=Conductor()
                    events,_=feed(engine,0,16,bpm,fps)
                    self.assertEqual(len(events),int(16*bpm/60))
                    self.assertAlmostEqual(engine.bpm,bpm,delta=0.3)
                    self.assertGreater(engine.confidence,0.95)
                    self.assertTrue(all(abs(b.t-a.t-60/bpm)<0.008 for a,b in zip(events[2:],events[3:])))

    def test_tempo_changes_converge_in_rolling_window(self):
        engine=Conductor()
        feed(engine,0,10,60)
        self.assertAlmostEqual(engine.bpm,60,delta=0.5)
        _, estimates=feed(engine,10,10,120)
        self.assertAlmostEqual(engine.bpm,120,delta=0.5)
        self.assertTrue(any(t<14 and abs(bpm-120)<1 for t,bpm in estimates))
        _,estimates=feed(engine,20,12,60)
        self.assertAlmostEqual(engine.bpm,60,delta=0.5)
        self.assertTrue(any(t<27 and abs(bpm-60)<1 for t,bpm in estimates))

    def test_jitter_and_irregular_measured_timestamps(self):
        engine=Conductor()
        events,_=feed(engine,0,20,120,fps=35,jitter=0.015,irregular=True)
        self.assertEqual(len(events),40)
        self.assertAlmostEqual(engine.bpm,120,delta=2)
        self.assertGreater(engine.confidence,0.75)

    def test_stationary_jitter_never_arms(self):
        engine=Conductor()
        rng=random.Random(4)
        for i in range(1200):
            self.assertIsNone(engine.update(i/60,0.5,0.62+rng.uniform(-0.015,0.015)))
        self.assertEqual(engine.count,0)
        self.assertIsNone(engine.bpm)

    def test_duplicate_and_out_of_order_timestamps_do_not_change_state(self):
        engine=Conductor()
        for i in range(600):
            t=i/60
            y=0.51+0.28*math.cos(2*math.pi*t)
            engine.update(t,0.5,y)
            before=(engine.count,engine.bpm,engine.smoothed,engine.armed,engine.status)
            self.assertIsNone(engine.update(t,0.5,1-y))
            self.assertIsNone(engine.update(t-0.01,None,None))
            self.assertEqual(before,(engine.count,engine.bpm,engine.smoothed,engine.armed,engine.status))
        self.assertEqual(engine.count,10)

    def test_duplicate_positions_do_not_make_extra_beats(self):
        engine=Conductor()
        for i in range(300):
            t=i/30
            y=0.51+0.28*math.cos(2*math.pi*t)
            engine.update(t,0.5,y)
            engine.update(t+1/60,0.5,y)
        self.assertEqual(engine.count,10)
        self.assertAlmostEqual(engine.bpm,60,delta=0.4)

    def test_lost_track_clears_tempo_and_cannot_beat_on_reacquire(self):
        engine=Conductor()
        feed(engine,0,8,120)
        count=engine.count
        engine.update(8,None,None)
        self.assertIsNone(engine.bpm)
        self.assertFalse(engine.armed)
        for i in range(60):
            self.assertIsNone(engine.update(8.1+i/60,0.5,0.8))
        self.assertEqual(engine.count,count)
        events,_=feed(engine,10,5,120)
        self.assertGreater(len(events),5)
        self.assertAlmostEqual(engine.bpm,120,delta=1)

    def test_reset_does_not_reuse_old_beats_or_allow_low_reacquisition(self):
        engine=Conductor()
        feed(engine,0,8,60)
        engine.reset()
        for i in range(60):
            self.assertIsNone(engine.update(8+i/60,0.5,0.85))
        self.assertEqual(engine.count,0)
        self.assertIsNone(engine.bpm)

    def test_long_frame_gap_and_position_jump_disarm(self):
        for discontinuity in ("gap","jump"):
            engine=Conductor()
            feed(engine,0,6,120)
            t=7 if discontinuity=="gap" else 6
            engine.update(t,0.99 if discontinuity=="jump" else 0.5,0.85)
            self.assertFalse(engine.armed)
            self.assertIsNone(engine.bpm)

    def test_pause_expires_and_needs_fresh_intervals(self):
        engine=Conductor()
        feed(engine,0,10,60)
        count=engine.count
        for i in range(240):
            engine.update(10+i/60,0.5,0.79)
        self.assertIsNone(engine.bpm)
        self.assertEqual(engine.count,count)
        self.assertFalse(engine.armed)
        feed(engine,14,2,60)
        self.assertIsNone(engine.bpm)
        feed(engine,16,6,60)
        self.assertAlmostEqual(engine.bpm,60,delta=1)

    def test_refractory_rejects_fast_repeated_full_cycles(self):
        cfg=DetectorConfig(refractory_s=0.29,smoothing_tau_s=0.005)
        engine=Conductor(cfg)
        events,_=feed(engine,0,5,600,fps=240)
        self.assertTrue(all(b.t-a.t>=0.29 for a,b in zip(events,events[1:])))

    def test_hysteresis_requires_upper_return_between_beats(self):
        engine=Conductor()
        feed(engine,0,2,60)
        count=engine.count
        for i in range(600):
            engine.update(2+i/60,0.5,0.62+0.02*math.sin(i/5))
        self.assertEqual(engine.count,count)

    def test_robust_interval_median_ignores_single_valid_outlier(self):
        engine=Conductor()
        engine.intervals.extend([0.5,0.5,0.5,0.9,0.5,0.5,0.5])
        self.assertAlmostEqual(engine.bpm,120)
        self.assertLess(engine.confidence,1)

    def test_invalid_coordinates_and_timestamps(self):
        engine=Conductor()
        feed(engine,0,6,120)
        engine.update(float("nan"),0.5,0.7)
        self.assertIsNotNone(engine.bpm)
        engine.update(6,0.5,float("nan"))
        self.assertIsNone(engine.bpm)
        self.assertFalse(engine.armed)

    def test_out_of_range_slow_tempo_never_publishes_bpm(self):
        engine=Conductor()
        feed(engine,0,30,25)
        self.assertIsNone(engine.bpm)

    def test_configuration_validation(self):
        for kwargs in ({"rearm_distance":0.8},{"smoothing_tau_s":0},{"min_bpm":200},
                       {"max_bpm":float("nan")},{"min_intervals":1},
                       {"window_intervals":3.5},{"refractory_s":0.9}):
            with self.subTest(kwargs=kwargs),self.assertRaises(ValueError):
                DetectorConfig(**kwargs)


class MetronomeTests(unittest.TestCase):
    def test_frequency_at_60_and_120(self):
        for bpm in (60,120):
            metro=TempoMetronome()
            ticks=sum(metro.update(i/1000,bpm) for i in range(10000))
            self.assertEqual(ticks,bpm//6)

    def test_tempo_changes_preserve_phase_and_duplicates_ignore(self):
        metro=TempoMetronome()
        metro.update(0,60)
        metro.update(0.25,60)
        metro.update(0.5,120)
        self.assertAlmostEqual(metro.phase,0.5)
        self.assertFalse(metro.update(0.5,60))
        self.assertEqual(metro.bpm,120)
        self.assertTrue(metro.update(0.75,120))
        self.assertAlmostEqual(metro.phase,0)

    def test_lost_tempo_stops_and_clears_oscillator(self):
        metro=TempoMetronome()
        metro.update(0,120)
        metro.update(0.2,None)
        self.assertFalse(metro.running)
        self.assertEqual(metro.phase,0)


class HandSelectorTests(unittest.TestCase):
    def test_keeps_initial_hand_when_other_hand_has_higher_score(self):
        selector=HandSelector()
        left=Hand(0.4,0.5,"Left",0.9)
        right=Hand(0.7,0.5,"Right",0.99)
        self.assertIs(selector.choose([left]),left)
        self.assertIs(selector.choose([right,left]),left)
        self.assertIsNone(selector.choose([right]))
        # Losing the target to a known competing hand is ambiguous until R.
        self.assertIsNone(selector.choose([left]))
        selector.reset()
        self.assertIs(selector.choose([right]),right)

    def test_jump_returns_loss_before_new_location(self):
        selector=HandSelector()
        selector.choose([Hand(0.1,0.2,"Left",0.9)])
        self.assertIsNone(selector.choose([Hand(0.9,0.8,"Left",0.9)]))


if __name__ == "__main__":
    unittest.main()
