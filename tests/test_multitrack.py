"""Deterministic end-to-end dual-track regressions, with no camera/model."""
import json
import math
import random
from types import SimpleNamespace
import unittest

from conductor.multitrack import ConductorSession, MultiHandTracker
from conductor.tracking import Hand


def hand(x=.25,y=.5,label="Right"):
    return Hand(x,y,label,.99,[SimpleNamespace(x=x,y=y) for _ in range(21)])


def wave(t,bpm=60,x=.25,label="Right",phase=0):
    return hand(x,.51+.28*math.cos(2*math.pi*(t*bpm/60+phase)),label)


def drive(session,start,end,source,rate=30):
    beats=[]
    for i in range(round(start*rate),round(end*rate)):
        t=i/rate
        beat=session.update(source(t),t)
        if beat:
            beats.append(beat)
    return beats


class MultiTrackTests(unittest.TestCase):
    def test_either_label_or_no_label_can_start(self):
        for label in ("Left","Right",None):
            s=ConductorSession()
            drive(s,0,1,lambda t:[hand(label=label)])
            self.assertEqual(s.primary,"A")
            self.assertEqual(s.hand.label,label)

    def test_two_initial_hands_both_visible_wait_for_explicit_primary(self):
        s=ConductorSession()
        drive(s,0,1,lambda t:[hand(.2),hand(.8,label="Left")])
        self.assertEqual(set(s.tracker.tracks),{"A","B"})
        self.assertIsNone(s.primary)
        self.assertTrue(s.select_primary("B"))
        s.update([hand(.2),hand(.8)],1.1)
        self.assertEqual(s.primary,"B")
        self.assertAlmostEqual(s.hand.x,.8)

    def test_first_screen_position_only_names_slots(self):
        s=ConductorSession()
        s.update([hand(.8),hand(.2)],0)
        self.assertEqual(s.tracker.tracks["A"].hand.x,.2)
        self.assertEqual(s.tracker.tracks["B"].hand.x,.8)

    def test_result_order_and_label_flips_do_not_break_either_track(self):
        s=ConductorSession()
        drive(s,0,1,lambda t:[hand(.2)])
        tokens=None
        for i in range(30,180):
            a=wave(i/30,60,.2,"Left" if i%2 else "Right")
            b=wave(i/30,90,.8,"Right" if i%2 else "Left")
            s.update([a,b] if i%2 else [b,a],i/30)
            current={k:v.token for k,v in s.tracker.tracks.items()}
            if tokens is not None:
                self.assertEqual(current,tokens)
            tokens=current
            self.assertEqual(s.primary,"A")
        self.assertGreater(s.engine.diagnostic_counts["selection_status:tracking_label_changed"],0)

    def test_secondary_entry_and_exit_preserves_primary_tempo(self):
        s=ConductorSession()
        drive(s,0,12,lambda t:[wave(t)]+([wave(t,120,.8)] if 3<t<8 else []))
        self.assertAlmostEqual(s.engine.bpm,60,delta=.2)
        self.assertEqual(s.engine.diagnostic_counts["reset:primary_track_segment_ended"],0)
        self.assertEqual(len(s.trajectories["A"].segments),1)

    def test_two_offset_hands_do_not_double_60_or_120_bpm(self):
        for bpm in (60,120):
            single,dual=ConductorSession(),ConductorSession()
            a=drive(single,0,12,lambda t:[wave(t,bpm)])
            b=drive(dual,0,12,lambda t:[wave(t,bpm)]+([wave(t,bpm,.8,phase=.5)] if t>1 else []))
            self.assertEqual([x.t for x in a],[x.t for x in b])
            self.assertAlmostEqual(dual.engine.bpm,bpm,delta=.2)

    def test_secondary_fast_tempo_cannot_change_main(self):
        s=ConductorSession()
        drive(s,0,15,lambda t:[wave(t,60)]+([wave(t,150,.8)] if t>1 else []))
        self.assertAlmostEqual(s.engine.bpm,60,delta=.2)

    def test_manual_primary_switch_resets_and_follows_new_tempo(self):
        s=ConductorSession()
        source=lambda t:[wave(t,60),wave(t,90,.8)]
        s.update(source(0),0)
        s.select_primary("A")
        drive(s,1/30,8,source)
        self.assertAlmostEqual(s.engine.bpm,60,delta=.2)
        self.assertTrue(s.select_primary("B"))
        self.assertIsNone(s.engine.bpm)
        self.assertIsNone(s.engine.previous_beat)
        beats=drive(s,8,18,source)
        self.assertIsNone(beats[0].interval)
        self.assertAlmostEqual(s.engine.bpm,90,delta=.3)

    def test_selecting_unavailable_track_is_noop(self):
        s=ConductorSession()
        drive(s,0,7,lambda t:[wave(t)])
        old=s.primary_token
        self.assertFalse(s.select_primary("B"))
        self.assertEqual(s.primary_token,old)
        self.assertAlmostEqual(s.engine.bpm,60,delta=.2)

    def test_same_primary_key_does_not_reset_beats(self):
        s=ConductorSession()
        drive(s,0,7,lambda t:[wave(t)])
        count=s.engine.count
        s.select_primary("A")
        self.assertEqual(s.engine.count,count)
        self.assertAlmostEqual(s.engine.bpm,60,delta=.2)

    def test_any_length_absence_either_hand_any_location_auto_recovers(self):
        for delay in (.033,1,20,3600):
            for label in ("Left","Right"):
                s=ConductorSession()
                drive(s,0,7,lambda t:[wave(t)])
                old=s.primary_token
                s.update([],7)
                self.assertIsNone(s.engine.output_bpm)
                self.assertIsNone(s.engine.previous_beat)
                s.update([hand(.9,.8,label)],7+delay)
                self.assertIsNone(s.primary)
                for i in range(1,20):
                    self.assertIsNone(s.update([hand(.9,.8,label)],7+delay+i/30))
                self.assertEqual(s.primary,"A")
                self.assertNotEqual(s.primary_token,old)
                self.assertEqual(s.engine.count,0)

    def test_swap_hands_after_absence_has_no_inherited_interval(self):
        s=ConductorSession()
        drive(s,0,8,lambda t:[wave(t,60,label="Left")])
        s.update([],8)
        new=drive(s,9,16,lambda t:[wave(t,120,.8,"Right")])
        self.assertIsNone(new[0].interval)
        self.assertAlmostEqual(s.engine.bpm,120,delta=.3)
        self.assertTrue(all(b.t>9 for b in new))

    def test_far_replacement_without_missing_frame_starts_fresh_segment(self):
        s=ConductorSession()
        drive(s,0,7,lambda t:[wave(t,60,.1)])
        old=s.primary_token
        s.update([hand(.95,.8)],7)
        self.assertIsNone(s.engine.bpm)
        drive(s,7.033333333,8,lambda t:[hand(.95,.8)])
        self.assertIsNotNone(s.primary)
        self.assertNotEqual(old,s.primary_token)
        self.assertEqual(s.engine.count,0)

    def test_remaining_secondary_auto_becomes_new_primary_after_delay(self):
        s=ConductorSession()
        drive(s,0,7,lambda t:[wave(t)]+([wave(t,90,.8)] if t>1 else []))
        s.update([wave(7,90,.8)],7)
        self.assertIsNone(s.primary)
        self.assertIsNone(s.engine.output_bpm)
        self.assertEqual(set(s.tracker.tracks),{"B"})
        beats=drive(s,7+1/30,14,lambda t:[wave(t,90,.8)])
        self.assertEqual(s.primary,"B")
        self.assertIsNone(beats[0].interval)
        self.assertAlmostEqual(s.engine.bpm,90,delta=.3)

    def test_primary_brief_exit_then_return_with_two_tracks_needs_choice(self):
        s=ConductorSession()
        drive(s,0,3,lambda t:[hand(.2)]+([hand(.8)] if t>1 else []))
        s.update([hand(.8)],3)
        s.update([hand(.2),hand(.8)],3.05)
        self.assertEqual(len(s.tracker.tracks),2)
        self.assertIsNone(s.primary)
        self.assertEqual(s.route_reason,"choose_primary_1_or_2")
        self.assertTrue(s.select_primary("A"))

    def test_crossing_pauses_before_touch_and_single_resumes_without_reset(self):
        s=ConductorSession()
        drive(s,0,1,lambda t:[hand(.2)])
        ambiguous_at=None
        for i in range(16):
            t=1+i/30
            a,b=hand(.2+.02*i),hand(.8-.02*i)
            beat=s.update([a,b] if i%2 else [b,a],t)
            self.assertIsNone(beat)
            if s.tracker.last_reason.startswith("ambiguous"):
                ambiguous_at=t
                self.assertIsNone(s.primary)
                break
        self.assertIsNotNone(ambiguous_at)
        for i in range(1,30):
            self.assertIsNone(s.update([hand(.75,.8)],ambiguous_at+i/30))
        self.assertIsNotNone(s.primary)
        self.assertEqual(s.engine.diagnostic_counts["reset:manual_reset"],0)

    def test_separated_pair_after_ambiguity_draws_both_but_waits_for_main(self):
        s=ConductorSession()
        drive(s,0,1,lambda t:[hand(.2)])
        s.update([hand(.48),hand(.52)],1)
        drive(s,1+1/30,2,lambda t:[hand(.2),hand(.8)])
        self.assertEqual(len(s.tracker.tracks),2)
        self.assertIsNone(s.primary)
        self.assertTrue(s.select_primary("B"))

    def test_merged_midpoint_ends_both_tracks_then_auto_recovers(self):
        s=ConductorSession()
        s.update([hand(.3),hand(.7)],0)
        old={v.token for v in s.tracker.tracks.values()}
        s.update([hand(.5)],.05)
        self.assertEqual(s.tracker.last_reason,"ambiguous_assignment")
        self.assertFalse(s.tracker.tracks)
        drive(s,.1,1,lambda t:[hand(.5)])
        self.assertIsNotNone(s.primary)
        self.assertNotIn(s.primary_token,old)

    def test_ambiguity_never_connects_old_trails_to_recovery(self):
        s=ConductorSession()
        drive(s,0,1,lambda t:[hand(.2,t*.1+.2),hand(.8,.7-t*.1)])
        s.update([hand(.49),hand(.51)],1)
        drive(s,1+1/30,2,lambda t:[hand(.2),hand(.8)])
        for history in s.trajectories.values():
            edges=list(history.edges(2,8))
            self.assertTrue(edges)
            self.assertFalse(any(a.t<1<b.t for a,b in edges))
            self.assertEqual(len(history.segments),2)

    def test_one_frame_gap_breaks_trail_and_clears_all_prior_intervals(self):
        s=ConductorSession()
        drive(s,0,8,lambda t:[wave(t)])
        s.update([],8)
        self.assertEqual(len(s.engine.intervals),0)
        drive(s,8+1/30,9,lambda t:[wave(t)])
        self.assertFalse(any(a.t<8<b.t for a,b in s.trajectories["A"].edges(9,8)))

    def test_long_camera_gap_ends_segment_without_explicit_missing_sample(self):
        s=ConductorSession()
        drive(s,0,7,lambda t:[wave(t)])
        old=s.primary_token
        self.assertIsNone(s.update([hand(.25,.8)],20))
        self.assertIsNone(s.primary)
        self.assertIsNone(s.engine.previous_beat)
        drive(s,20+1/30,21,lambda t:[hand(.25,.8)])
        self.assertNotEqual(s.primary_token,old)
        self.assertEqual(s.engine.count,0)

    def test_duplicate_and_out_of_order_frames_do_not_move_tracks_or_add_beats(self):
        s=ConductorSession()
        drive(s,0,7,lambda t:[wave(t)])
        old=s.primary_token
        count=s.engine.count
        last=s.last_input
        for t in (last,last-.1,float("nan")):
            self.assertIsNone(s.update([hand(.95,.9)],t))
        self.assertEqual(s.primary_token,old)
        self.assertEqual(s.engine.count,count)
        self.assertEqual(s.last_input,last)

    def test_invalid_observation_pauses_then_single_recovers_automatically(self):
        for invalid in (float("nan"),-1,2):
            s=ConductorSession()
            drive(s,0,1,lambda t:[hand()])
            s.update([hand(),hand(invalid)],1)
            self.assertIsNone(s.primary)
            drive(s,1+1/30,2,lambda t:[hand()])
            self.assertIsNotNone(s.primary)

    def test_manual_reset_clears_both_histories_and_selection(self):
        s=ConductorSession()
        drive(s,0,3,lambda t:[hand(.2)]+([hand(.8)] if t>1 else []))
        s.reset()
        self.assertIsNone(s.primary)
        self.assertFalse(s.tracker.tracks)
        self.assertTrue(all(not x.segments for x in s.trajectories.values()))
        drive(s,3,4,lambda t:[hand(.8,label="Left")])
        self.assertIsNotNone(s.primary)

    def test_initial_label_hint_does_not_block_opposite_hand_reentry(self):
        s=ConductorSession(preference="Right")
        drive(s,0,1,lambda t:[hand(.2,label="Left"),hand(.8,label="Right")])
        self.assertEqual(s.primary,"B")
        s.update([],1)
        drive(s,1+1/30,2,lambda t:[hand(.4,label="Left")])
        self.assertEqual(s.hand.label,"Left")

    def test_stale_result_cannot_establish_or_move_tracks(self):
        s=ConductorSession()
        drive(s,0,1,lambda t:[hand()])
        result=SimpleNamespace(hand_landmarks=[[SimpleNamespace(x=.9,y=.8)]],handedness=[])
        accepted,beat=s.consume_result(result,1,2)
        self.assertFalse(accepted)
        self.assertIsNone(beat)
        self.assertFalse(s.tracker.tracks)
        self.assertIsNone(s.primary)

    def test_nonmonotonic_result_does_not_mutate_existing_track(self):
        s=ConductorSession()
        drive(s,0,1,lambda t:[hand()])
        token=s.primary_token
        result=SimpleNamespace(hand_landmarks=[[SimpleNamespace(x=.9,y=.8)]],handedness=[])
        accepted,beat=s.consume_result(result,.5,1)
        self.assertFalse(accepted)
        self.assertEqual(s.primary_token,token)

    def test_diagnostics_contain_labels_and_segment_ids_but_no_coordinates(self):
        s=ConductorSession()
        drive(s,0,1,lambda t:[hand(.2),hand(.8)])
        values=s.details()
        self.assertEqual(values["visible_tracks"],2)
        self.assertIn("track_a_generation",values)
        self.assertFalse(set(values)&{"x","y","landmarks","frames","image"})
        for event in s.engine.drain_events():
            self.assertFalse(set(event)&{"x","y","landmarks","frames","image"})
        json.dumps(values,allow_nan=False)

    def test_secondary_history_starts_new_segment_after_return(self):
        s=ConductorSession()
        drive(s,0,3,lambda t:[hand(.2)]+([hand(.8)] if .5<t<1.5 or t>2 else []))
        self.assertEqual(len(s.trajectories["A"].segments),1)
        self.assertEqual(len(s.trajectories["B"].segments),2)

    def test_tempo_change_with_two_tracks_remains_single_source(self):
        s=ConductorSession()
        def source(t):
            phase=t if t<8 else 8+(t-8)*1.5
            a=hand(.2,.51+.28*math.cos(2*math.pi*phase),"Left" if int(t*30)%2 else "Right")
            return [a]+([wave(t,120,.8)] if t>1 else [])
        drive(s,0,8,source)
        self.assertAlmostEqual(s.engine.bpm,60,delta=.2)
        drive(s,8,20,source)
        self.assertAlmostEqual(s.engine.bpm,90,delta=.3)

    def test_tracker_rejects_more_than_two_without_sticky_lock(self):
        tracker=MultiHandTracker()
        self.assertFalse(tracker.update([hand(.1),hand(.5),hand(.9)],0))
        self.assertEqual(len(tracker.update([hand(.5)],.05)),1)

    def test_dual_track_timing_with_jitter_dropped_and_duplicate_frames(self):
        for fps in (18,20,24,30):
            for bpm in (60,120):
                s=ConductorSession()
                rng=random.Random(37)
                t=0
                beats=[]
                while t<16:
                    a=wave(t,bpm,.2,"Left" if rng.random()<.5 else "Right")
                    jitter=rng.uniform(-.004,.004)
                    a.y+=jitter
                    for point in a.landmarks:
                        point.y+=jitter
                    candidates=[a]+([wave(t,90,.8)] if t>1 else [])
                    event=s.update(candidates,t)
                    if event:
                        beats.append(event)
                    self.assertIsNone(s.update(candidates,t))
                    dt=(1+rng.uniform(-.15,.15))/fps
                    t+=dt*(2 if rng.random()<.04 else 1)
                self.assertAlmostEqual(s.engine.bpm,bpm,delta=1.5)
                self.assertTrue(all(b.t-a.t>.28 for a,b in zip(beats,beats[1:])))
                self.assertLessEqual(len(beats),round(16*bpm/60))

    def test_beat_diagnostics_identify_exactly_one_primary_generation(self):
        s=ConductorSession()
        drive(s,0,6,lambda t:[wave(t)]+([wave(t,120,.8)] if t>1 else []))
        events=[e for e in s.engine.drain_events() if e["event"]=="beat"]
        self.assertTrue(events)
        self.assertEqual({e["primary_track"] for e in events},{"A"})
        self.assertEqual({e["primary_generation"] for e in events},{s.primary_token[1]})

    def test_stationary_two_hand_jitter_never_makes_beats(self):
        s=ConductorSession()
        rng=random.Random(9)
        s.update([hand(.2),hand(.8)],0)
        s.select_primary("A")
        for i in range(1,600):
            observations=[hand(.2,.62+rng.uniform(-.008,.008)),hand(.8,.62+rng.uniform(-.008,.008))]
            self.assertIsNone(s.update(observations,i/30))
        self.assertEqual(s.engine.count,0)


if __name__=="__main__":
    unittest.main()
