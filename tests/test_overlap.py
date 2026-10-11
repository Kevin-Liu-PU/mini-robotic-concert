"""Hand-authored full-hand geometry and synthetic adversarial variants."""
from copy import deepcopy
from dataclasses import replace
import json
import math
from pathlib import Path
import unittest

from conductor.core import DetectorConfig
from conductor.multitrack import ConductorSession, MultiHandTracker, Track
from conductor.overlap import choose_overlap, geometry
from conductor.tracking import AssociationConfig
from tests.support.replay import hands

from tests.support.synthetic_fixtures import overlap_rows
FIXTURE=overlap_rows()
ROWS=FIXTURE['rows']
TARGET=next(r for r in ROWS if r['sequence']==FIXTURE['target_sequence'])


def transform(hand,dx=0,dy=0,mirror=False,rotate=False):
    result=deepcopy(hand)
    for p in result.landmarks:
        if mirror:p.x=2*hand.x-p.x
        if rotate:p.x,p.y=hand.x-(p.y-hand.y),hand.y+(p.x-hand.x)
        p.x+=dx;p.y+=dy
    result.x,result.y=result.landmarks[0].x,result.landmarks[0].y
    return result


def prepared(config=None):
    session=ConductorSession(detector_config=config)
    for row in ROWS:
        if row['time']>=TARGET['time']:break
        session.update(hands(row['candidates']),row['time'])
    assert session.primary_token is not None
    return session


class OverlapTests(unittest.TestCase):
    def test_recovers_synthetic_observed_crossing_and_keeps_following_tip(self):
        session=prepared();token=session.primary_token;pair=hands(TARGET['candidates'])
        beat=session.update(pair,TARGET['time'])
        self.assertIsNotNone(beat)
        self.assertAlmostEqual(beat.t,FIXTURE['expected_contact_time'])
        self.assertIs(session.hand,pair[0])
        self.assertEqual(session.primary_token,token)
        self.assertEqual(session.tracker.last_reason,'main_continues_overlapping_hypotheses')
        self.assertIsNone(session.tracker.competitor)
        following=next(r for r in ROWS if r['time']>TARGET['time'])
        # Its tip is closer to the old alternative: that alone is not identity.
        actual=hands(following['candidates'])[0]
        self.assertLess(math.dist(actual.index_tip,pair[1].index_tip),math.dist(actual.index_tip,pair[0].index_tip))
        self.assertIsNone(session.update([actual],following['time']))
        self.assertEqual(session.primary_token,token)
        self.assertIs(session.hand,actual)

    def test_candidate_order_labels_and_scores_do_not_select_identity(self):
        for reverse in (False,True):
            for labels in (('Left','Right'),('Right','Left'),(None,None)):
                s=prepared();pair=hands(TARGET['candidates'])
                for h,label,score in zip(pair,labels,(.01,.9999)):
                    h.label=label;h.label_score=score
                winner=pair[0]
                beat=s.update(pair[::-1] if reverse else pair,TARGET['time'])
                self.assertIs(s.hand,winner)
                self.assertAlmostEqual(beat.t,FIXTURE['expected_contact_time'])

    def test_post_beat_overlap_retains_count_without_an_extra_beat(self):
        # Move the contact line earlier within the same synthetic downstroke.
        s=prepared(DetectorConfig(beat_y=.592));token=s.primary_token
        previous=s.engine.previous_beat;count=s.engine.count
        self.assertIsNotNone(previous)
        self.assertLess(TARGET['time']-previous,.12)
        self.assertIsNone(s.update(hands(TARGET['candidates']),TARGET['time']))
        self.assertEqual(s.primary_token,token)
        self.assertEqual(s.engine.previous_beat,previous)
        self.assertEqual(s.engine.count,count)

    def rejected(self,pair,session=None):
        s=session or prepared()
        s.update(pair,TARGET['time'])
        self.assertNotEqual(s.tracker.last_reason,'main_continues_overlapping_hypotheses')
        return s

    def test_startup_and_unselected_track_cannot_use_overlap(self):
        for s in (ConductorSession(),prepared()):
            s.primary_token=None
            self.rejected(hands(TARGET['candidates']),s)
            self.assertIsNone(s.primary)

    def test_incomplete_invalid_and_degenerate_geometry_has_no_exemption(self):
        for bad in ('missing','nan','invalid_tip','outside','collapsed','zero_width','bad_wrist','long_chain'):
            pair=hands(TARGET['candidates']);h=pair[1]
            if bad=='missing':h.landmarks=h.landmarks[:9]
            if bad=='nan':h.landmarks[12].z=float('nan')
            if bad=='invalid_tip':h.landmarks[8].x=float('nan')
            if bad=='outside':h.landmarks[12].x=1.1
            if bad=='collapsed':h.landmarks=[deepcopy(h.landmarks[0]) for _ in range(21)]
            if bad=='zero_width':h.landmarks[17]=deepcopy(h.landmarks[5])
            if bad=='bad_wrist':h.landmarks[0].x+=.01
            if bad=='long_chain':h.landmarks[7].x=.01
            with self.subTest(bad=bad):
                self.assertIsNone(geometry(h))
                self.assertIsNone(self.rejected(pair).primary)

    def test_same_shape_translated_mirrored_rotated_are_not_duplicates(self):
        for options in ({'dx':.06},{'mirror':True},{'rotate':True}):
            pair=hands(TARGET['candidates'])
            pair[1]=transform(pair[0],**options)
            with self.subTest(options=options):
                s=self.rejected(pair)
                self.assertIsNone(s.primary)

    def test_candidates_on_opposite_contact_sides_remain_uncertain(self):
        pair=hands(TARGET['candidates'])
        # Move both candidate tips near the line, leaving the same close palms.
        pair[0].landmarks[8].y=.600
        pair[1].landmarks[8].y=.616
        s=self.rejected(pair)
        self.assertEqual(s.tracker.last_decision['overlap_check'],'tip_actions_disagree')
        self.assertIsNone(s.primary)

    def test_equal_hypotheses_never_use_array_order_as_tiebreak(self):
        pair=hands(TARGET['candidates']);pair[1]=deepcopy(pair[0])
        s=self.rejected(pair)
        self.assertEqual(s.tracker.last_decision['overlap_check'],'motion_hypotheses_not_distinguishable')
        self.assertIsNone(s.primary)

    def test_established_second_track_protection_still_applies(self):
        s=prepared();old=s.tracker.tracks[s.primary]
        other=transform(old.hand,dx=.18)
        s.tracker.tracks['B']=Track('B',100,other,old.started_at,old.t)
        s.update(hands(TARGET['candidates']),TARGET['time'])
        self.assertIsNone(s.primary)
        self.assertEqual(s.tracker.last_reason,'ambiguous_close_wrists')
        self.assertEqual(s.tracker.last_decision['overlap_check'],'not_applicable')

    def test_recent_missing_secondary_and_unresolved_competitors_block_exemption(self):
        for shift in (.04,.18):
            s=prepared();old=s.tracker.tracks[s.primary]
            secondary=transform(old.hand,dx=shift)
            s.tracker.competitor=secondary,old.t
            s.update(hands(TARGET['candidates']),TARGET['time'])
            self.assertIsNone(s.primary)
            self.assertEqual(s.tracker.last_decision['overlap_check'],'not_applicable')

    def test_pause_and_reacquire_cannot_erase_recent_two_track_observation(self):
        for reset in (False,True):
            s=prepared();old=s.tracker.tracks[s.primary]
            s.tracker.tracks['B']=Track('B',100,transform(old.hand,dx=.18),old.started_at,old.t)
            last_two=old.t
            s.update(hands(TARGET['candidates']),TARGET['time'])
            self.assertIsNone(s.primary)
            self.assertEqual(s.tracker.recent_two_track_time,last_two)
            if reset:s.reset()
            # Replay the synthetic lead-in at new measured times, long enough to
            # confirm a new MAIN but still within the last B observation's TTL.
            lead=[r for r in ROWS if TARGET['time']-.35 <= r['time'] < TARGET['time']]
            shift=.399936
            for row in lead:s.update(hands(row['candidates']),row['time']+shift)
            self.assertIsNotNone(s.primary)
            now=TARGET['time']+shift
            self.assertLess(now-last_two,s.config.distractor_memory_s)
            s.update(hands(TARGET['candidates']),now)
            self.assertIsNone(s.primary)
            self.assertEqual(s.tracker.last_decision['overlap_check'],'not_applicable')
            self.assertEqual(s.tracker.recent_two_track_time,last_two)

    def test_exact_previous_alternative_cannot_silently_replace_main(self):
        s=prepared();pair=hands(TARGET['candidates'])
        s.update(pair,TARGET['time'])
        s.update([deepcopy(pair[1])],TARGET['time']+.03)
        self.assertIsNone(s.primary)
        self.assertEqual(s.tracker.last_reason,'ambiguous_overlap_replacement')
        self.assertIsNone(s.engine.previous_beat)

    def test_replacement_guard_applies_to_two_candidates_and_fallback_too(self):
        # Adding another copy of a rejected
        # pose must not turn an invalid single-candidate replacement into a pass.
        for reverse in (False,True):
            for dt in (.025,.03,.035,.04):
                for kind in ('overlap','far_fallback','close_fallback'):
                    s=prepared();pair=hands(TARGET['candidates']);s.update(pair,TARGET['time'])
                    replacement=deepcopy(pair[1]);other=deepcopy(replacement)
                    if kind=='overlap':
                        for i in (6,7,8):other.landmarks[i].x+=.015;other.landmarks[i].y+=.035
                    elif kind=='far_fallback':other=transform(other,dx=-.35)
                    else:other=transform(other,dx=-.10)
                    candidates=[replacement,other]
                    s.update(candidates[::-1] if reverse else candidates,TARGET['time']+dt)
                    with self.subTest(reverse=reverse,dt=dt,kind=kind):
                        self.assertIsNone(s.primary)
                        self.assertIsNone(s.engine.previous_beat)

    def test_repeat_overlap_then_separation_cannot_keep_old_exemption(self):
        s=prepared();pair=hands(TARGET['candidates']);s.update(pair,TARGET['time'])
        main=transform(pair[0],dx=.03,dy=.06)
        other=transform(main,dx=.005)
        for i in (6,7,8):other.landmarks[i].y+=.06
        s.update([main,other],TARGET['time']+.032)
        self.assertEqual(s.tracker.last_reason,'main_continues_overlapping_hypotheses')
        separated=transform(other,dx=.06)
        s.update([transform(main,dx=.01,dy=.02),separated],TARGET['time']+.064)
        self.assertNotEqual(s.tracker.last_reason,'main_continues_overlapping_hypotheses')

    def test_occlusion_long_gap_and_reset_clear_overlap_evidence(self):
        for kind in ('empty','gap','reset'):
            s=prepared();pair=hands(TARGET['candidates']);s.update(pair,TARGET['time'])
            self.assertIsNotNone(s.tracker.overlap_alternative)
            if kind=='empty':s.update([],TARGET['time']+.03)
            if kind=='gap':s.update([pair[0]],TARGET['time']+1)
            if kind=='reset':s.reset()
            self.assertIsNone(s.tracker.overlap_alternative)
            self.assertIsNone(s.primary)
            self.assertIsNone(s.engine.previous_beat)

    def test_duplicate_frame_does_not_reapply_overlap_or_refresh_evidence(self):
        s=prepared();pair=hands(TARGET['candidates']);s.update(pair,TARGET['time'])
        before=(s.primary_token,s.engine.count,s.tracker.overlap_alternative)
        self.assertIsNone(s.update(pair[::-1],TARGET['time']))
        self.assertEqual(before,(s.primary_token,s.engine.count,s.tracker.overlap_alternative))

    def test_normal_displacement_guard_is_not_relaxed(self):
        s=prepared();s.tracker.max_jump=.001
        s.update(hands(TARGET['candidates']),TARGET['time'])
        self.assertIsNone(s.primary)

    def test_geometry_separation_revokes_exemption(self):
        for dx in (.02,.04,.08,.15):
            s=prepared();pair=hands(TARGET['candidates'])
            pair[1]=transform(pair[1],dx=dx)
            self.rejected(pair,s)

    def test_stationary_or_direction_reversing_history_is_insufficient(self):
        for kind in ('static','reverse','sparse'):
            s=prepared();old=s.tracker.tracks[s.primary]
            if kind=='static':
                old.observations[1]=(old.observations[1][0],deepcopy(old.observations[0][1]))
            if kind=='reverse':
                time,hand=old.observations[1];hand=deepcopy(hand);hand.landmarks[8].y=.9
                old.observations[1]=(time,hand)
            if kind=='sparse':
                time,hand=old.observations[0];old.observations[0]=(time-.3,hand)
            self.rejected(hands(TARGET['candidates']),s)
            self.assertIsNone(s.primary)

    def test_single_after_overlap_still_cannot_jump_to_alternative_location(self):
        s=prepared();pair=hands(TARGET['candidates']);s.update(pair,TARGET['time'])
        s.tracker.max_jump=.001
        actual=hands(next(r for r in ROWS if r['time']>TARGET['time'])['candidates'])
        s.update(actual,TARGET['time']+.04)
        self.assertIsNone(s.primary)

    def test_bad_ratio_configuration_is_rejected(self):
        for value in (0,1,float('nan'),True):
            with self.assertRaises(ValueError):AssociationConfig(overlap_motion_ratio=value)


if __name__=='__main__':unittest.main()
