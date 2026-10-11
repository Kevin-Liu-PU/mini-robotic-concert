"""Slow observed intervals, guarded compact returns, and explanatory UI; no devices."""
from dataclasses import replace
import json
import math
from pathlib import Path
import random
import tempfile
import unittest

from conductor.app import create_screen_clock
from conductor.core import Conductor, DetectorConfig
from conductor.screen_display import follow_feedback
from conductor.settings import load_config
from tests.test_phase_reentry import advance

ROOT = Path(__file__).resolve().parents[1]


def config():
    return load_config(ROOT/'config-responsive.json')


def wave(engine, bpm, seconds, *, amplitude=.035, fps=30, start=0):
    events=[]
    for i in range(round(seconds*fps)):
        t=start+i/fps
        y=engine.config.contact_y+amplitude*math.cos(2*math.pi*i/fps*bpm/60)
        event=engine.update(t,.5,y)
        if event:events.append(event)
    return events


class SlowFollowTests(unittest.TestCase):
    def engine(self):
        return Conductor(DetectorConfig(**config()['detector']))

    def test_main_profile_ranges_timeouts_and_old_profile(self):
        cfg=config();d=DetectorConfig(**cfg['detector']);c=create_screen_clock(cfg)
        self.assertEqual((d.min_bpm,c.cfg.min_bpm,d.max_bpm,c.cfg.max_bpm),(25,25,180,180))
        self.assertGreater(d.stale_min_s,60/d.min_bpm+d.max_frame_gap_s)
        self.assertEqual((d.max_frame_gap_s,d.brief_loss_s,c.cfg.heartbeat_timeout_s,c.cfg.result_max_age_s),(.25,.12,.5,.25))
        old=load_config(ROOT/'tests/fixtures/config-before-slow.json')
        self.assertEqual(old['detector']['min_bpm'],40)
        self.assertEqual(DetectorConfig(**old['detector']).compact_rearm_margin,0)

    def test_mismatched_range_and_short_stale_timeout_refused(self):
        for field,value in [('min_bpm',26),('stale_min_s',1.8)]:
            cfg=config();cfg['detector'][field]=value
            with tempfile.TemporaryDirectory() as folder:
                path=Path(folder)/'config.json';path.write_text(json.dumps(cfg))
                with self.assertRaises(ValueError):load_config(path)

    def test_compact_small_motion_at_25_30_40_60_120(self):
        for bpm in (25,30,40,60,120):
            for fps in (20,30,60):
                with self.subTest(bpm=bpm,fps=fps):
                    engine=self.engine();events=wave(engine,bpm,24,amplitude=.035,fps=fps)
                    expected=math.floor(24*bpm/60+.25)
                    # Startup acquisition may consume the first compact cycle.
                    self.assertIn(len(events),(expected-1,expected))
                    self.assertTrue(all(abs(b.t-a.t-60/bpm)<.025 for a,b in zip(events,events[1:])))
                    self.assertAlmostEqual(engine.bpm,bpm,delta=.5)
                    self.assertEqual(engine.diagnostic_counts['reset:tempo_expired'],0)

    def test_full_amplitude_60_120_180_keeps_one_contact_per_cycle(self):
        for bpm in (60,120,180):
            engine=self.engine();events=wave(engine,bpm,12,amplitude=.22)
            self.assertIn(len(events),(12*bpm//60-1,12*bpm//60))
            self.assertTrue(all(abs(b.t-a.t-60/bpm)<.025 for a,b in zip(events,events[1:])))

    def test_band_edge_jitter_never_arms_in_20_seed_runs(self):
        for seed in range(20):
            e=self.engine();rng=random.Random(seed)
            for i in range(900):
                self.assertIsNone(e.update(i/30,.5,e.config.contact_y+rng.uniform(-.015,.015)))
            self.assertEqual(e.count,0)

    def test_one_frame_excursions_cannot_prepare_contact(self):
        e=self.engine()
        for i in range(1800):
            # 60 spatially plausible tip outliers: enough height, no dwell.
            y=e.config.contact_y-.025 if i%30==15 else e.config.contact_y+.01
            self.assertIsNone(e.update(i/30,.5,y))
        self.assertEqual(e.count,0)

    def test_duplicate_and_out_of_order_samples_do_not_satisfy_dwell(self):
        e=self.engine()
        for i in range(15):e.update(i/30,.5,.65)
        e.update(.5,.5,.582)
        for i in range(50):
            e.update(.5,.5,.582);e.update(.49,.5,.582)
        self.assertFalse(e.armed)
        self.assertIsNone(e.update(.533,.5,.63))
        self.assertEqual(e.count,0)

    def test_loss_breaks_partial_compact_preparation(self):
        e=self.engine()
        for i in range(15):e.update(i/30,.5,.65)
        e.update(.5,.5,.583);e.update(.533,.5,.581)
        e.update(.55,None,None)
        self.assertIsNone(e.update(.58,.5,.635))
        self.assertFalse(e.armed);self.assertEqual(e.count,0)

    def test_line_edit_clears_pending_small_return(self):
        e=self.engine()
        for i in range(15):e.update(i/30,.5,.65)
        e.update(.5,.5,.582)
        self.assertIsNotNone(e.compact_since)
        e.set_beat_line(.63)
        self.assertIsNone(e.compact_since);self.assertFalse(e.armed)

    def test_180_to_25_continuous_visibility_does_not_expire_motion(self):
        e=self.engine();wave(e,180,4,amplitude=.2)
        events=wave(e,25,15,amplitude=.2,start=4)
        self.assertGreaterEqual(len(events),6)
        self.assertEqual(e.diagnostic_counts['reset:tempo_expired'],0)

    def test_slow_valid_interval_updates_immediately(self):
        c=create_screen_clock(config());stamp=.5
        advance(c,.54,.5)
        for interval in (.5,1.5265476,2.058744,2.285983,2.4,1.5):
            stamp+=interval;advance(c,stamp+.04,stamp)
            self.assertAlmostEqual(c.period,interval)
            self.assertIsNone(c.snapshot(c.last_step)['tempo_feedback'])

    def test_below_25_is_reanchored_and_short_extra_keeps_anchor(self):
        c=create_screen_clock(config())
        advance(c,.54,.5);advance(c,1.54,1.5)
        advance(c,4.04,4.)
        self.assertEqual(c.period,1)
        self.assertIn('BELOW 25',follow_feedback(c.snapshot(c.last_step)))
        advance(c,6.44,6.4);self.assertAlmostEqual(c.period,2.4)
        advance(c,6.64,6.6);self.assertAlmostEqual(c.raw_anchor,6.4)
        self.assertIn('ABOVE 180',follow_feedback(c.snapshot(c.last_step)))

    def test_long_lost_interval_never_sets_slow_tempo(self):
        c=create_screen_clock(config())
        advance(c,.54,.5);advance(c,1.54,1.5)
        advance(c,3.7,tracked=False)
        self.assertIn('TRACK LOST',follow_feedback(c.snapshot(c.last_step)))
        advance(c,3.84,3.8)
        self.assertEqual(c.period,1)  # 2.3 seconds is in range, but crosses loss.
        advance(c,6.14,6.1);self.assertAlmostEqual(c.period,2.3)

    def test_missed_contact_with_continuous_tracking_is_ambiguous(self):
        c=create_screen_clock(config())
        advance(c,.54,.5);advance(c,1.54,1.5)
        # With no loss signal, one missed 60 BPM contact is indistinguishable
        # from an intentional 30 BPM interval under single-interval following.
        advance(c,3.54,3.5);self.assertEqual(c.period,2.)
        advance(c,4.54,4.5);self.assertEqual(c.period,1.)

    def test_duplicate_boundary_contact_never_doubles_the_clock(self):
        c=create_screen_clock(config())
        advance(c,.54,.5);advance(c,2.94,2.9)
        before=(c.observed_count,c.period)
        c.step(2.95,frame_time=c.last_frame,track_good=True,beat_time=2.91)
        self.assertEqual((c.observed_count,c.period),before)
        advance(c,2.96,2.9)
        self.assertEqual((c.observed_count,c.period),before)

    def test_ui_distinguishes_not_prepared_from_loss_and_stop(self):
        c=create_screen_clock(config());advance(c,.54,.5)
        g=dict(armed=False,rejection=dict(reason='not_armed'))
        self.assertIn('not prepared',follow_feedback(c.snapshot(c.last_step),g))
        c.stop(.55)
        self.assertIn('Enter',follow_feedback(c.snapshot(.55),g))
        c.fail(.56,'TEST_FAULT')
        self.assertIn('safety watchdog',follow_feedback(c.snapshot(.56),g))

    def test_rejection_visible_but_expires_and_reset_clears_it(self):
        e=self.engine()
        for i in range(20):e.update(i/30,.5,.65)
        e.update(.7,.5,.60);e.update(.75,.5,.63)
        self.assertEqual(e.feedback(.8)['rejection']['reason'],'not_armed')
        self.assertIsNone(e.feedback(4)['rejection'])
        e.reset();self.assertIsNone(e.feedback(4)['rejection'])


if __name__=='__main__':unittest.main()
