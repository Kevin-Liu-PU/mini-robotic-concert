"""Constructed spatial counterexamples, NOT trajectories recovered from trial logs."""
import itertools
import unittest

from conductor.core import DetectorConfig
from conductor.multitrack import ConductorSession
from tests.test_tip_anchor import drive, moving, observation


class AssociationAdversarialTests(unittest.TestCase):
    def stable(self, **kwargs):
        s = ConductorSession(**kwargs)
        drive(s,0,8,lambda t:[moving(t)])
        self.assertAlmostEqual(s.engine.bpm,60,delta=.3)
        return s

    def stopped(self,s):
        self.assertIsNone(s.primary)
        self.assertIsNone(s.engine.output_bpm)
        self.assertIsNone(s.engine.previous_beat)
        self.assertFalse(s.engine.intervals)

    def test_joint_support_without_independent_competitor_ratio_all_orderings(self):
        for first_reverse,second_reverse in itertools.product((False,True),repeat=2):
            with self.subTest(first_reverse=first_reverse,second_reverse=second_reverse):
                s=self.stable()
                token=s.primary_token
                first=[moving(8),observation(x=.34)]
                s.update(first[::-1] if first_reverse else first,8)
                main=observation(x=.26)
                second=[main,observation(x=.306)]
                s.update(second[::-1] if second_reverse else second,8+1/30)
                self.assertEqual(s.primary_token,token)
                self.assertIs(s.hand,main)
                self.assertEqual(s.tracker.last_decision['ownership_mode'],'main_and_memory_joint_assignment')
                self.assertLess(.08/.034,3)  # Old independent memory check failed.
                self.assertGreater(.136/.044,3)  # Complete assignments distinguish.

    def test_persistent_secondary_then_only_secondary_cannot_inherit_main(self):
        for reverse in (False,True):
            s=self.stable()
            token=s.primary_token
            for i in range(240,360):
                pair=[moving(i/30),observation(.8,x=.30)]
                s.update(pair[::-1] if (i%2)==reverse else pair,i/30)
                self.assertEqual(s.primary_token,token)
                self.assertFalse(s.trajectories['B'].segments)
            self.assertIsNone(s.update([observation(.8,x=.30)],12))
            self.stopped(s)
            drive(s,12+1/30,13,lambda t:[observation(.8,x=.30)])
            self.assertNotEqual(s.primary_token,token)
            self.assertEqual(s.engine.count,0)
            self.assertFalse(any(a.t<12<=b.t for a,b in s.trajectories['A'].edges(13,8)))

    def test_joint_ownership_is_consistent_across_close_radius_boundary(self):
        for alternate_x in (.344,.345,.346,.35):
            for first_reverse,second_reverse in itertools.product((False,True),repeat=2):
                with self.subTest(alternate_x=alternate_x,first_reverse=first_reverse,second_reverse=second_reverse):
                    s=self.stable()
                    token=s.primary_token
                    pair=[moving(8),observation(x=.34)]
                    s.update(pair[::-1] if first_reverse else pair,8)
                    main=observation(x=.225)
                    pair=[main,observation(x=alternate_x)]
                    s.update(pair[::-1] if second_reverse else pair,8+1/30)
                    self.assertEqual(s.primary_token,token)
                    self.assertIs(s.hand,main)
                    self.assertEqual(s.tracker.last_decision['ownership_mode'],'main_and_memory_joint_assignment')
                    if alternate_x-.225>s.config.ambiguity_radius:
                        self.assertEqual(len(s.tracker.tracks),2)
                        self.assertIsNone(s.tracker.competitor)
                    else:
                        self.assertEqual(len(s.tracker.tracks),1)

    def test_memory_does_not_make_close_duplicates_distinguishable(self):
        for offset in (0,.001,.01,.019):
            for reverse in (False,True):
                s=self.stable()
                s.update([moving(8),observation(x=.30)],8)
                pair=[observation(x=.25),observation(x=.25+offset)]
                self.assertIsNone(s.update(pair[::-1] if reverse else pair,8+1/30))
                self.stopped(s)

    def test_promoted_secondary_uses_current_track_without_stale_memory_veto(self):
        for reverse in (False,True):
            for vanish in ('main','secondary_then_replacement','secondary_then_main'):
                with self.subTest(reverse=reverse,vanish=vanish):
                    s=self.stable()
                    token=s.primary_token
                    for i,x in enumerate((.34,.40,.48)):
                        main=moving(8+i/30)
                        pair=[main,observation(.8,x=x)]
                        s.update(pair[::-1] if reverse else pair,8+i/30)
                        self.assertEqual(s.primary_token,token)
                        self.assertIs(s.hand,main)
                        if i:
                            self.assertEqual(len(s.tracker.tracks),2)
                            self.assertIsNone(s.tracker.competitor)
                    if vanish=='main':
                        self.assertIsNone(s.update([observation(.8,x=.48)],8+3/30))
                        self.stopped(s)
                    else:
                        s.update([moving(8+3/30)],8+3/30)
                        self.assertEqual(s.primary_token,token)
                        self.assertIsNotNone(s.tracker.competitor)
                        self.assertAlmostEqual(s.tracker.competitor[1],8+2/30)
                        if vanish=='secondary_then_replacement':
                            self.assertIsNone(s.update([observation(.8,x=.48)],8+4/30))
                            self.stopped(s)
                        else:
                            drive(s,8+4/30,9,lambda t:[moving(t)])
                            self.assertEqual(s.primary_token,token)
                            self.assertIsNone(s.tracker.competitor)

    def test_crossed_memory_and_merged_single_stop_in_every_order(self):
        for reverse in (False,True):
            for xs in ((.25,.20),(.275,),(.30,)):
                s=self.stable()
                s.update([moving(8),observation(x=.30)],8)
                candidates=[observation(x=x) for x in xs]
                self.assertIsNone(s.update(candidates[::-1] if reverse else candidates,8+1/30))
                self.stopped(s)

    def test_two_established_tracks_crossing_never_uses_sole_main_exception(self):
        for reverse in (False,True):
            s=self.stable()
            drive(s,8,9,lambda t:[moving(t),observation(x=.75)])
            for i in range(18):
                pair=[observation(x=.25+.015*i),observation(x=.75-.015*i)]
                s.update(pair[::-1] if reverse else pair,9+i/30)
                if s.primary is None:
                    self.assertEqual(s.tracker.last_decision['continuity_check'],'requires_sole_prior_track')
                    break
            self.stopped(s)

    def test_accelerating_single_hand_then_near_candidate_birth_keeps_observed_main(self):
        for reverse in (False,True):
            s=self.stable()
            token=s.primary_token
            xs=(.25,.28,.34,.44,.45,.455,.46,.462)
            for i,x in enumerate(xs):
                t=8+i/30
                main=moving(t,x)
                candidates=[main]
                if i>=4:
                    candidates.append(observation(.8,x=x+.07))
                s.update(candidates[::-1] if reverse else candidates,t)
                self.assertEqual(s.primary_token,token)
                self.assertIs(s.hand,main)
            self.assertEqual(s.engine.diagnostic_counts['reset:primary_track_segment_ended'],0)
            self.assertEqual(len(s.trajectories['A'].segments),1)

    def test_current_motion_limits_remain_active_with_unique_joint_score(self):
        for max_jump,step in ((.30,.04),(.01,.015)):
            s=ConductorSession(detector_config=DetectorConfig(max_position_jump=max_jump))
            drive(s,0,8,lambda t:[observation(.55)])
            self.assertIsNotNone(s.primary)
            # Large separation gives a >3x distance advantage, but the current
            # sole-MAIN cap (.03) or a stricter configured guard must still stop.
            s.update([observation(x=.25+step),observation(x=.25+step+.115)],8)
            self.stopped(s)

    def test_infeasible_memory_match_cannot_force_current_ownership(self):
        s=self.stable()
        s.update([moving(8),observation(x=.34)],8)
        s.update([observation(x=.26),observation(x=.9)],8+1/30)
        self.stopped(s)
        self.assertEqual(s.tracker.last_decision['ownership_check'],
                         'winning_hypothesis_outside_displacement_guard')

    def test_processing_gap_without_empty_observation_preserves_identity(self):
        s=self.stable()
        token=s.primary_token
        s.update([moving(8+.067)],8+.067)
        self.assertEqual(s.primary_token,token)
        self.assertEqual(s.engine.diagnostic_counts['reset:primary_track_segment_ended'],0)

    def test_short_explicit_loss_clears_intervals_and_never_bridges_recovery(self):
        s=self.stable()
        token=s.primary_token
        s.update([moving(8),observation(x=.30)],8)
        self.assertIsNone(s.update([],8+1/30))
        self.stopped(s)
        drive(s,8+2/30,9,lambda t:[observation(.8)])
        self.assertNotEqual(s.primary_token,token)
        self.assertEqual(s.engine.count,0)
        self.assertFalse(any(a.t<8+1/30<b.t for a,b in s.trajectories['A'].edges(9,8)))

    def test_long_gap_with_old_positions_never_revives_old_generation(self):
        s=self.stable()
        token=s.primary_token
        self.assertIsNone(s.update([observation(.8)],10))
        self.stopped(s)
        drive(s,10+1/30,11,lambda t:[observation(.8)])
        self.assertNotEqual(s.primary_token,token)
        self.assertEqual(s.engine.count,0)


if __name__=='__main__':
    unittest.main()
