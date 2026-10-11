"""Observed-interval following and unchanged safety gates; no hardware."""
import unittest
from conductor.responsive_clock import ResponsiveScreenClock
from conductor.audio_clock import AudioPlanner


def advance(c, end, beat=None, tracked=True):
    if c.last_step is None:c.step(0,frame_time=0,track_good=tracked)
    while c.last_step+.02<end-1e-9:
        t=c.last_step+.02;c.step(t,frame_time=t,track_good=tracked)
    return c.step(end,frame_time=end,track_good=tracked,beat_time=beat)


def stable(period=1):
    c=ResponsiveScreenClock()
    for i in range(5):
        stamp=.5+i*period;advance(c,stamp+.04,stamp)
    c.drain_events()
    return c


class ResponsiveClockTests(unittest.TestCase):
    def test_initial_two_contacts_one_interval(self):
        c=ResponsiveScreenClock()
        advance(c,.54,.5);self.assertFalse(c.running)
        advance(c,1.29,1.25)
        self.assertTrue(c.running);self.assertAlmostEqual(c.period,.75)
        self.assertEqual(c.required_fresh_intervals,1)

    def test_each_new_interval_is_used_immediately(self):
        c=stable();stamp=c.raw_anchor
        for period in (.75,.5,1.,1.5,1/3,.8):
            stamp+=period;advance(c,stamp+.04,stamp)
            self.assertAlmostEqual(c.period,period)
            self.assertEqual(c.state,'MEASURED')

    def test_slowdown_not_broken_by_old_fast_period_timeout(self):
        c=stable(1/3);last=c.raw_anchor
        advance(c,last+1.54,last+1.5)
        self.assertAlmostEqual(c.period,1.5)

    def test_duplicate_contact_and_duplicate_frame_do_not_update(self):
        c=stable();before=(c.period,c.observed_count)
        c.step(c.last_step+.01,frame_time=c.last_frame,track_good=True,beat_time=c.raw_anchor+.01)
        self.assertEqual(before,(c.period,c.observed_count))
        advance(c,c.last_step+.02,c.raw_anchor)
        self.assertEqual(before,(c.period,c.observed_count))

    def test_too_short_extra_keeps_original_valid_anchor(self):
        c=stable();last=c.raw_anchor
        advance(c,last+.24,last+.2)
        self.assertEqual(c.raw_anchor,last);self.assertEqual(c.period,1)
        advance(c,last+1.04,last+1)
        self.assertAlmostEqual(c.period,1)

    def test_plausible_missing_contact_is_ambiguous_with_slowdown(self):
        c=stable(.5);last=c.raw_anchor
        advance(c,last+1.04,last+1)
        self.assertEqual(c.period,1)  # Do not secretly infer an intended 120 BPM.
        advance(c,last+1.54,last+1.5)
        self.assertEqual(c.period,.5)

    def test_missing_60_contact_is_outside_supported_range_and_held(self):
        c=stable();last=c.raw_anchor
        advance(c,last+2.04,last+2)
        self.assertEqual(c.period,1)
        advance(c,last+3.04,last+3)
        self.assertEqual(c.period,1)

    def test_plausible_midpoint_extra_can_change_tempo(self):
        c=stable();last=c.raw_anchor
        advance(c,last+.54,last+.5)
        self.assertEqual(c.period,.5)

    def test_tracking_loss_never_bridges_interval_and_keeps_output(self):
        c=stable();now=c.last_step;predictions=c.predicted_count
        advance(c,now+3,tracked=False)
        self.assertTrue(c.running);self.assertGreater(c.predicted_count,predictions)
        advance(c,now+3.1,now+3.06)
        self.assertEqual(c.period,1)
        advance(c,now+3.85,now+3.81)
        self.assertAlmostEqual(c.period,.75)

    def test_another_loss_clears_partial_return_anchor(self):
        c=stable();now=c.last_step
        advance(c,now+.1,tracked=False)
        advance(c,now+.2,now+.16)
        advance(c,now+.3,tracked=False)
        advance(c,now+.8,now+.76)
        self.assertEqual(c.period,1)
        advance(c,now+1.55,now+1.51)
        self.assertAlmostEqual(c.period,.75)

    def test_line_change_keeps_schedule_and_needs_new_pair(self):
        c=stable();now=c.last_step;schedule=c.next_tick
        c.beat_line_changed(now+.001)
        self.assertEqual(c.next_tick,schedule);self.assertTrue(c.running)
        advance(c,now+.2,now+.16);self.assertEqual(c.period,1)
        advance(c,now+.95,now+.91);self.assertAlmostEqual(c.period,.75)

    def test_space_and_fault_latches_survive_line_changes(self):
        for fault in (False,True):
            c=stable();now=c.last_step
            if fault:c.fail(now,'CAMERA_ERROR')
            else:c.stop(now)
            c.beat_line_changed(now+.001)
            advance(c,now+1,now+.96)
            self.assertFalse(c.running);self.assertFalse(c.armed)
            if fault:self.assertEqual(c.fault_reason,'CAMERA_ERROR')

    def test_enter_resumes_old_tempo_and_new_pair_updates(self):
        c=stable();now=c.last_step;c.stop(now);self.assertTrue(c.arm(now+.001))
        self.assertTrue(c.running);self.assertEqual(c.snapshot(now)['required_resume_intervals'],1)
        advance(c,now+.2,now+.16);self.assertTrue(c.resume_pending)
        advance(c,now+.95,now+.91);self.assertFalse(c.resume_pending)
        self.assertAlmostEqual(c.period,.75)

    def test_reset_stays_stopped_until_arm_and_clears_period(self):
        c=stable();now=c.last_step;c.reset(now)
        advance(c,now+1,now+.96)
        self.assertFalse(c.running);self.assertIsNone(c.period)
        self.assertTrue(c.arm(now+1.001));self.assertFalse(c.running)

    def test_heartbeat_fault_still_latches(self):
        c=stable();now=c.last_step
        c.step(now+.7,frame_time=now+.7,track_good=True)
        self.assertEqual(c.fault_reason,'PROCESSING_HEARTBEAT_GAP')
        self.assertFalse(c.running)

    def test_direct_phase_correction_preserves_pending_identity_and_spacing(self):
        c=stable();now=c.last_step;previous=c.period
        c.next_tick=now+.35;c.last_tick=now-.1;c.period=.75
        count=c.predicted_count;c._align(now-.04,now,previous)
        e=c.drain_events()[-1]
        self.assertAlmostEqual(e['requested_correction_s'],e['error_s'])
        self.assertGreaterEqual(c.next_tick,c.last_tick+.6*.75)
        self.assertEqual(c.predicted_count,count)

    def test_mute_stays_independent_of_line_and_tempo_changes(self):
        c=stable();p=AudioPlanner();now=c.last_step
        self.assertFalse(p.publish(c,now).active)
        p.toggle_mute(now);self.assertTrue(p.publish(c,now).active)
        generation=p.generation;c.beat_line_changed(now+.001)
        plan=p.publish(c,now+.001,c.drain_events())
        self.assertTrue(plan.active);self.assertEqual(plan.generation,generation)


if __name__=='__main__':unittest.main()
