"""Offline behavioral regressions for bounded phase reacquisition."""
import unittest
import numpy as np
from conductor.app import create_screen_clock
from conductor.audio_clock import AudioConfig, AudioPlanner, ClickRenderer
from conductor.phase_clock import ReentryResponsiveScreenClock
from conductor.responsive_clock import ResponsiveScreenClock


def advance(clock, end, beat=None, tracked=True):
    if clock.last_step is None:
        clock.step(0, frame_time=0, track_good=tracked)
    while clock.last_step+.01 < end-1e-9:
        t=clock.last_step+.01
        clock.step(t, frame_time=t, track_good=tracked)
    return clock.step(end, frame_time=end, track_good=tracked, beat_time=beat)


def stable(cls=ReentryResponsiveScreenClock):
    c=cls()
    for stamp in [.5,1.5,2.5,3.5]:advance(c,stamp+.04,stamp)
    c.drain_events()
    return c


class ReentryPhaseTests(unittest.TestCase):
    def test_regular_ramps_and_steps_are_identical_to_original_responsive(self):
        a,b=stable(ResponsiveScreenClock),stable()
        stamp=3.5
        for interval in [.9,.8,.7,.6,.5,.6,.8,1.,1.5,1/3,.75,1.]:
            stamp+=interval
            for c in [a,b]:advance(c,stamp+.04,stamp)
            self.assertEqual((a.period,a.next_tick,a.predicted_count,a.last_tick),
                             (b.period,b.next_tick,b.predicted_count,b.last_tick))

    def test_return_can_rephase_without_claiming_a_new_tempo(self):
        c=stable();advance(c,4.9,tracked=False)
        evidence=c.last_accepted;count=c.predicted_count
        advance(c,5.19,5.15)
        self.assertEqual(c.period,1)
        self.assertEqual(c.last_accepted,evidence)
        self.assertEqual(c.snapshot(5.19)['current_evidence_confidence'],0)
        self.assertEqual(c.next_tick,5.19)
        self.assertEqual(c.predicted_count,count)
        decisions=[e for e in c.drain_events() if e['kind']=='REENTRY_PHASE_DECISION']
        self.assertEqual(sum(e['accepted'] for e in decisions),1)
        advance(c,5.25)
        self.assertAlmostEqual(c.next_tick,6.15)
        self.assertEqual(c.predicted_count,count+1)

    def test_second_contact_still_sets_tempo_immediately(self):
        c=stable();advance(c,4.9,tracked=False);advance(c,5.19,5.15)
        advance(c,5.94,5.9)
        self.assertAlmostEqual(c.period,.75)
        self.assertFalse(c.reentry)

    def test_line_edit_alone_does_not_trigger_the_return_cue(self):
        a,b=stable(ResponsiveScreenClock),stable()
        for c in [a,b]:
            c.beat_line_changed(3.6)
            advance(c,4.19,4.15)
        self.assertEqual(a.next_tick,b.next_tick)
        self.assertFalse(any(e['kind']=='REENTRY_PHASE_DECISION' for e in b.drain_events()))

    def test_half_period_return_keeps_spacing_instead_of_double_click(self):
        c=stable();advance(c,4.9,tracked=False)
        advance(c,5.04,5.0)
        decision=[e for e in c.drain_events() if e['kind']=='REENTRY_PHASE_DECISION'][-1]
        self.assertFalse(decision['accepted'])
        self.assertEqual(decision['reason'],'RECENT_TICK_KEEP_SPACING')
        self.assertAlmostEqual(c.next_tick,5.5)

    def test_already_sounded_prediction_is_not_replayed(self):
        c=stable();advance(c,4.55,tracked=False)
        pending=c.next_tick
        advance(c,4.64,4.60)
        self.assertEqual(c.next_tick,pending)
        self.assertFalse([e for e in c.drain_events() if e['kind']=='REENTRY_PHASE_DECISION'][-1]['accepted'])

    def test_full_audio_lead_horizon_is_protected(self):
        for remaining in [.005,.05,.199,.20]:
            c=stable();c._begin_reentry();now=5.
            c.next_tick=now+remaining;c.last_tick=now-.8
            c._estimate(now-.04,now)
            self.assertEqual(c.next_tick,now+remaining)
            self.assertIsNone(c.return_cue_anchor)

    def test_committed_audio_prediction_has_one_identity_and_one_click(self):
        c=stable();c._begin_reentry();c.next_tick=5.2;c.last_tick=4.2
        c.last_frame=c.last_processed=5.04
        p=AudioPlanner();p.toggle_mute(5.04)
        r=ClickRenderer(AudioConfig());buf=np.empty((480,1),np.float32)
        r.plan=p.publish(c,5.04)
        r.render(buf,480,now=5.04,pa_current=105.04,pa_dac=105.196)
        self.assertEqual(r.requested_count,1)
        c._estimate(5.02,5.06)
        self.assertEqual(c.next_tick,5.2)
        r.plan=p.publish(c,5.06,c.drain_events())
        r.render(buf,480,now=5.05,pa_current=105.05,pa_dac=105.206)
        self.assertEqual(r.requested_count,1)
        self.assertIsNone(r.fault)

    def test_space_and_fault_suppress_the_pending_return_cue(self):
        for fault in [False,True]:
            c=stable();advance(c,4.9,tracked=False);advance(c,5.19,5.15)
            if fault:c.fail(5.191,'CAMERA_ERROR')
            else:c.stop(5.191)
            p=AudioPlanner();p.toggle_mute(5.192)
            r=ClickRenderer(AudioConfig());buf=np.empty((480,1),np.float32)
            r.plan=p.publish(c,5.192,c.drain_events())
            r.render(buf,480,now=5.193,pa_current=105.193,pa_dac=105.223)
            self.assertEqual(r.requested_count,0)
            self.assertFalse(np.any(buf))
            advance(c,6.2,6.15)
            self.assertFalse(c.running)

    def test_duplicate_frame_cannot_create_another_return_cue(self):
        c=stable();advance(c,4.9,tracked=False);advance(c,5.19,5.15)
        c.drain_events()
        c.step(5.195,frame_time=c.last_frame,track_good=True,beat_time=5.16)
        self.assertFalse(any(e['kind']=='REENTRY_PHASE_DECISION' for e in c.drain_events()))

    def test_reset_clears_pending_anchor_and_stays_stopped(self):
        c=stable();advance(c,4.9,tracked=False);advance(c,5.19,5.15)
        c.reset(5.195)
        self.assertIsNone(c.return_cue_anchor)
        self.assertFalse(c.running)
        self.assertIsNone(c.period)

    def test_factory_uses_allowed_driver_lead_and_preserves_rollback(self):
        cfg={'follow_policy':'responsive','phase_policy':'reentry'}
        c=create_screen_clock(cfg,AudioConfig(max_output_lead_s=.25))
        self.assertIsInstance(c,ReentryResponsiveScreenClock)
        self.assertEqual(c.commit_horizon_s,.25)
        self.assertIs(type(create_screen_clock({'follow_policy':'responsive'})),ResponsiveScreenClock)
        with self.assertRaises(ValueError):create_screen_clock({'phase_policy':'reentry'})


if __name__=='__main__':unittest.main()
