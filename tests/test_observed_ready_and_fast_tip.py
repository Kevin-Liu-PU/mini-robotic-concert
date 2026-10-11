"""Synthetic prepared contacts and adversarial discontinuities; camera-free."""
import json
import math
from pathlib import Path
from types import SimpleNamespace
import unittest

from conductor.core import Conductor
from conductor.multitrack import ConductorSession
from conductor.tracking import Hand


def hand(x=.5,y=.3,wrist_y=.65):
    pts=[None]*9;pts[8]=SimpleNamespace(x=x,y=y)
    return Hand(x,wrist_y,'Left',.9,pts)


class ObservedReadyTests(unittest.TestCase):
    def test_current_high_observations_prepare_first_new_downstroke(self):
        c=Conductor();events=[]
        for i in range(11):
            e=c.update(i*.05,.5,.3 if i<7 else .3+(i-6)*.10)
            if e:events.append(e)
        self.assertEqual(len(events),1);self.assertIsNone(events[0].interval)
        self.assertEqual(c.diagnostic_counts['armed:observed_ready_after_acquisition'],1)

    def test_holding_high_or_low_never_makes_a_beat(self):
        for y in (.3,.8):
            c=Conductor()
            for i in range(100):self.assertIsNone(c.update(i*.04,.5,y))

    def test_loss_during_downstroke_cannot_be_bridged(self):
        c=Conductor()
        for i in range(8):c.update(i*.05,.5,.3)
        c.update(.4,None,None)
        self.assertIsNone(c.update(.45,.5,.8))
        self.assertIsNone(c.previous_beat)
        for i in range(10):self.assertIsNone(c.update(.5+i*.05,.5,.8))

    def test_crossing_before_acquisition_finishes_does_not_wait_to_be_counted(self):
        c=Conductor();c.update(0,.5,.3)
        c.update(.05,.5,.5);c.update(.1,.5,.65)
        for i in range(20):self.assertIsNone(c.update(.15+i*.04,.5,.65))
        self.assertEqual(c.count,0)

    def test_band_jitter_cannot_rearm(self):
        c=Conductor()
        for i in range(150):self.assertIsNone(c.update(i*.04,.5,.615+.018*math.sin(i)))

    def test_new_high_hand_can_prepare_without_inheriting_old_interval(self):
        s=ConductorSession()
        for i in range(60):s.update([hand(.15,.3)],i*.03)
        s.update([hand(.15,.5)],1.8);s.update([hand(.15,.7)],1.86)
        self.assertIsNotNone(s.engine.previous_beat)
        self.assertIsNone(s.update([hand(.9,.8)],1.92))
        self.assertIsNone(s.engine.previous_beat)
        self.assertIsNone(s.engine.bpm)
        for i in range(12):self.assertIsNone(s.update([hand(.9,.8)],1.95+i*.04))

    def test_two_established_tracks_cross_and_cannot_supply_a_synthetic_beat(self):
        s=ConductorSession()
        for i in range(20):s.update([hand(.2,.3),hand(.8,.3)],i*.04)
        self.assertTrue(s.select_primary('A'))
        for i in range(20,30):s.update([hand(.2,.3),hand(.8,.3)],i*.04)
        self.assertIsNone(s.update([hand(.49,.65),hand(.51,.65)],1.2))
        self.assertIsNone(s.primary);self.assertIsNone(s.engine.previous_beat)
        self.assertEqual(s.tracker.last_reason,'ambiguous_close_wrists')


class SupportedFastTipTests(unittest.TestCase):
    def prepared(self):
        c=Conductor()
        for i,y in enumerate((.26,.267,.284,.306,.321,.357,.411,.497)):
            c.update(i*.064,.5,y,wrist=(.55,.68))
        return c

    def test_continuing_fast_tip_with_stable_wrist_emits_once(self):
        c=self.prepared();e=c.update(.512,.515,.828,wrist=(.543,.687))
        self.assertIsNotNone(e)
        self.assertEqual(c.diagnostic_counts['motion_supported:continuous_tip_and_wrist'],1)
        self.assertEqual(c.diagnostic_counts['reset:position_jump'],0)
        self.assertIsNone(c.update(.512,.515,.828,wrist=(.543,.687)))
        self.assertIsNone(c.update(.576,.515,.828,wrist=(.543,.687)))
        self.assertEqual(c.count,1)

    def test_unsupported_wrist_lateral_reverse_and_large_jumps_still_reset(self):
        for x,y,wrist in ((.515,.828,(.65,.68)),(.84,.50,(.55,.68)),(.5,.10,(.55,.68)),(.5,.99,(.55,.68))):
            with self.subTest(x=x,y=y,wrist=wrist):
                c=self.prepared();self.assertIsNone(c.update(.512,x,y,wrist=wrist))
                self.assertEqual(c.diagnostic_counts['reset:position_jump'],1)

    def test_stationary_tip_cannot_justify_a_large_step(self):
        c=Conductor()
        for i in range(10):c.update(i*.06,.5,.3,wrist=(.55,.68))
        self.assertIsNone(c.update(.60,.5,.65,wrist=(.55,.68)))
        self.assertEqual(c.diagnostic_counts['reset:position_jump'],1)

    def test_supported_motion_is_stable_across_microsecond_and_realistic_dt_jitter(self):
        for jitter in (-.005,-.000001,0,.000001,.0001,.001,.005):
            with self.subTest(jitter=jitter):
                c=self.prepared()
                self.assertIsNotNone(c.update(.512+jitter,.515,.828,wrist=(.543,.687)))
                self.assertEqual(c.diagnostic_counts['reset:position_jump'],0)

    def test_missing_sample_and_long_gap_erase_support(self):
        for missing in (True,False):
            c=self.prepared()
            if missing:c.update(.48,None,None)
            self.assertIsNone(c.update(.512 if missing else .9,.515,.828,wrist=(.543,.687)))
            self.assertIsNone(c.previous_beat)
            self.assertEqual(c.diagnostic_counts['motion_supported:continuous_tip_and_wrist'],0)


class SyntheticContactTests(unittest.TestCase):
    def test_six_reacquired_contacts_and_four_continuous_contacts_are_preserved(self):
        from tests.support.synthetic_fixtures import contact_rows
        data=contact_rows()
        s=ConductorSession();beats=[];tokens=[]
        for row in data['rows']:
            hands=[]
            for c in row['candidates']:
                tip=c['tip8'];w=c['wrist'];pts=[None]*9
                pts[8]=SimpleNamespace(x=tip['x'] if tip['x'] is not None else float('nan'),y=tip['y'] if tip['y'] is not None else float('nan'))
                hands.append(Hand(w['x'] if w['x'] is not None else float('nan'),w['y'] if w['y'] is not None else float('nan'),c['label'],0,pts))
            e=s.update(hands,row['time'])
            if e:beats.append(e);tokens.append(s.primary_token)
        self.assertEqual(len(beats),10)
        for target in data['targets']:
            self.assertEqual(sum(abs(e.t-target)<.15 for e in beats),1,target)
        for target in data['prior_accepted']:
            self.assertTrue(any(abs(e.t-target)<1e-8 for e in beats),target)
        for i,(e,token) in enumerate(zip(beats,tokens)):
            if i==0 or token!=tokens[i-1]:self.assertIsNone(e.interval)
        self.assertEqual(s.engine.diagnostic_counts['reset:position_jump'],0)


if __name__=='__main__':unittest.main()
