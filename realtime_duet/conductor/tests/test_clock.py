import math
import unittest
from conductor_rt.clock import BeatObservation, MusicalClock
from conductor_rt.protocol import WireEncoder
from conductor_rt.runner import LatestFrame


def running(bpm=60, score_beats=48):
    c = MusicalClock(bpm, session_id="synthetic", score_beats=score_beats)
    c.arm(0)
    c.start(0)
    return c


def feed(c, stamps, delay=.035, quality=1.):
    records=[]
    events=[]
    for stamp in stamps:
        now=stamp+delay
        tick=(c.now or 0)+.005
        while tick < now:
            events += c.tick(tick)
            tick += .005
        records.append(c.observe(BeatObservation(stamp,now,quality),now))
    return records,events


class ClockTests(unittest.TestCase):
    def test_initial_bpm_required(self):
        with self.assertRaises(TypeError):
            MusicalClock()
        for x in (0,29,181,float("nan"),float("inf")):
            with self.assertRaises(ValueError):
                MusicalClock(x)

    def test_60_bpm_full_48_beats_has_twelve_unique_four_beat_onsets(self):
        c=running()
        indices=[0]
        for i in range(1,10000):
            indices += [e["beat_index"] for e in c.tick(i*.005) if e["kind"]=="beat"]
        self.assertEqual([x for x in indices if x%4==0],list(range(0,48,4)))
        self.assertEqual(indices,list(range(48)))
        self.assertEqual(c.state,"ended")
        self.assertEqual(c.phase,48)

    def test_missing_vision_holds_without_stop(self):
        c=running(90,score_beats=None)
        c.tick(10)
        self.assertEqual(c.state,"running")
        self.assertAlmostEqual(c.bpm,90)
        self.assertAlmostEqual(c.phase,15)
        self.assertEqual(c.message(10)["quality"]["mode"],"hold")

    def test_acceleration_and_slowdown_not_permanently_gated(self):
        for target in (120,65):
            c=running(90,score_beats=None)
            stamps=[i*60/90 for i in range(1,7)]
            stamps += [stamps[-1]+i*60/target for i in range(1,13)]
            records,events=feed(c,stamps)
            self.assertLess(abs(c.bpm-target),3)
            ids=[e["beat_index"] for e in events]
            self.assertEqual(ids,sorted(set(ids)))
            self.assertTrue(any(x["reason"]=="tempo_and_phase" for x in records))

    def test_double_speed_after_sustained_observations(self):
        c=running(60,score_beats=None)
        feed(c,list(range(1,7))+[6+i*.5 for i in range(1,14)])
        self.assertLess(abs(c.bpm-120),3)

    def test_tempo_change_relocks_same_beat_identity_without_cycle_slip(self):
        for target in (120,65):
            c=MusicalClock(90,score_beats=None)
            c.arm(-.1)
            c.start(0,observation=BeatObservation(0,0,frame=0))
            truth=[0.]
            for i in range(1,49):
                truth.append(truth[-1]+60/(90 if i<9 else target))
            records,events=feed(c,truth[1:])
            tail=[e for e in events if e["kind"]=="beat" and 35<=e["beat_index"]<len(truth)]
            self.assertGreater(len(tail),8)
            self.assertLess(max(abs(e["monotonic_ns"]/1e9-truth[e["beat_index"]]) for e in tail),.025)

    def test_missing_every_other_beat_not_half_speed(self):
        c=running(90,score_beats=None)
        feed(c,[i*60/90 for i in range(1,7)]+[4+i*120/90 for i in range(1,7)])
        self.assertAlmostEqual(c.bpm,90)
        self.assertEqual(c.tempo_evidence,"integer_gap_hold")

    def test_isolated_interval_error_does_not_change_tempo(self):
        c=running(90,score_beats=None)
        feed(c,[.6666667,1.3333333,2.,2.37,3.3333333,4.])
        self.assertLess(abs(c.bpm-90),.01)

    def test_duplicate_and_disordered_observations_rejected(self):
        c=running()
        feed(c,[1,2])
        for stamp in (2,1.5):
            r=c.observe(BeatObservation(stamp,2.1),2.1)
            self.assertEqual(r["reason"],"duplicate_or_out_of_order")

    def test_stale_invalid_low_confidence(self):
        c=running()
        for obs,reason in [(BeatObservation(.1,1),"future_or_stale"),
                           (BeatObservation(float("nan"),1),"invalid"),
                           (BeatObservation(.9,1,.2),"low_quality")]:
            self.assertEqual(c.observe(obs,1)["reason"],reason)

    def test_recovery_only_adjusts_future_phase(self):
        c=running(90,score_beats=None)
        c.tick(5)
        before=c.phase
        c.observe(BeatObservation(4.9,5),5)
        self.assertEqual(c.phase,before)
        c.tick(5.01)
        self.assertGreater(c.phase,before)

    def test_stop_priority_latches_and_no_same_session_restart(self):
        c=running()
        c.stop(.95,"test")
        phase=c.phase
        self.assertEqual(c.tick(2),[])
        self.assertEqual(c.arm(2),[])
        self.assertEqual(c.start(2),[])
        self.assertEqual(c.phase,phase)

    def test_automatic_start_rejects_stale_low_quality_and_before_prepare(self):
        cases=[BeatObservation(9.8,10.1,1.,frame=1),
               BeatObservation(10.01,10.02,.1,frame=2),
               BeatObservation(10.1,10.2,1.,frame=3),
               BeatObservation(10.6,10.6,1.,kind="internal_clock",frame=4),
               BeatObservation(10.6,10.6,1.,frame=5,predicted=True),
               BeatObservation(.6,10.6,1.,frame=6),
               BeatObservation(10.8,10.8,1.,frame=8),
               BeatObservation(float("nan"),10.6,1.,frame=7)]
        for obs in cases:
            c=MusicalClock(60)
            c.arm(10)
            self.assertEqual(c.start(10.61,"visual_first_downbeat",obs),[])
            self.assertEqual(c.state,"armed")
            self.assertEqual(c.last_beat,-1)

    def test_automatic_start_requires_real_fresh_observation_after_arm(self):
        c=MusicalClock(60)
        c.arm(100)
        out=c.start(100.2,"visual_first_downbeat",BeatObservation(100.1,100.18,.9,frame=12,source_s=1.2))
        self.assertEqual([x["kind"] for x in out],["start","beat"])
        self.assertEqual(c.state,"running")
        self.assertEqual(c.armed_at,100)

    def test_explicit_keyboard_start_is_separate_from_visual_start(self):
        c=MusicalClock(60)
        c.arm(100,"keyboard_prepare")
        out=c.start(100.01,"keyboard_start")
        self.assertEqual([x["kind"] for x in out],["start","beat"])

    def test_pts_is_not_a_monotonic_start_stamp(self):
        c=MusicalClock(60)
        c.arm(50000)
        out=c.start(50000.2,"visual_first_downbeat",BeatObservation(8.1,50000.18,.9,frame=10,source_s=8.1))
        self.assertEqual(out,[])
        self.assertEqual(c.state,"armed")

    def test_latest_prepare_invalidates_older_buffered_downbeat(self):
        c=MusicalClock(60)
        c.arm(10)
        c.arm(11)
        self.assertEqual(c.start(11.1,"visual_first_downbeat",BeatObservation(10.9,11.05,.9,frame=3)),[])
        self.assertEqual(c.armed_at,11)

    def test_automatic_start_without_evidence_is_not_manual_start(self):
        c=MusicalClock(60)
        c.arm(10)
        self.assertEqual(c.start(10.1,"visual_first_downbeat"),[])
        self.assertEqual(c.state,"armed")

    def test_predictions_never_become_visual_observations(self):
        c=running()
        record=c.observe(BeatObservation(.5,.6,predicted=True),.6)
        self.assertEqual(record["reason"],"not_visual_evidence")
        self.assertEqual(c.accepted,0)

    def test_minimum_tempo_wire_floor_survives_phase_slew(self):
        c=running(30)
        c.observe(BeatObservation(2.2,2.2),2.2)
        wire=WireEncoder("test").encode(c.message(2.2))
        self.assertGreaterEqual(wire["tempo_bpm"],30)

    def test_scheduler_stall_does_not_catch_up(self):
        c=running(60,score_beats=None)
        events=c.tick(4.01)
        self.assertEqual(len(events),1)
        self.assertEqual(events[0]["beat_index"],4)
        self.assertEqual(c.skipped_deadlines,3)

    def test_phase_is_monotonic_during_tempo_corrections(self):
        c=running(90,score_beats=None)
        phases=[]
        next_obs=.65
        for i in range(1,4000):
            now=i*.005
            c.tick(now)
            if now>=next_obs:
                c.observe(BeatObservation(now-.02,now),now)
                next_obs=now+(.5 if now<10 else .9)
            phases.append(c.phase)
        self.assertTrue(all(a<=b for a,b in zip(phases,phases[1:])))

    def test_wire_contract_and_stop(self):
        c=running()
        c.tick(.25)
        e=WireEncoder("shared-test")
        m=e.encode(c.message(.25))
        required={"version","session_id","clock_domain","sequence","source_monotonic_ns","kind","state","beat_position","tempo_bpm","meter","quality"}
        self.assertTrue(required<=m.keys())
        self.assertEqual(m["kind"],"CLOCK")
        self.assertEqual(m["beat_position"],.25)
        self.assertEqual(m["meter"],[4,4])
        c.stop(.5)
        s=e.encode(c.message(.5))
        self.assertEqual(s["kind"],"STOP")
        self.assertGreater(s["sequence"],m["sequence"])

    def test_frame_queue_is_bounded_latest_only(self):
        q=LatestFrame()
        for i in range(1000):
            q.put(i)
        self.assertEqual(q.take(),999)
        self.assertEqual(q.dropped,999)
        self.assertEqual(q.max_depth,1)


if __name__=="__main__":
    unittest.main()
