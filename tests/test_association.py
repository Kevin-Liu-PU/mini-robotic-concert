"""Spatial/temporal association regressions; every position is synthetic."""
import math
import unittest

from conductor.core import Conductor
from conductor.tracking import AssociationConfig,Hand,HandSelector


def hand(x=.5,y=.5,label='Right',score=.99):
    return Hand(x,y,label,score)


class AssociationTests(unittest.TestCase):
    def test_single_continuous_hand_survives_repeated_label_reversals(self):
        selector=HandSelector()
        for i in range(200):
            t=i/20
            observed=hand(.5+.03*math.sin(t),.5+.2*math.cos(t*3),'Left' if i%2 else 'Right')
            self.assertIs(selector.choose([observed],t),observed)
        self.assertFalse(selector.needs_reset)

    def test_first_label_never_becomes_permanent_identity(self):
        selector=HandSelector()
        selector.choose([hand(label='Left')],0)
        for i in range(1,20):
            h=hand(label='Right')
            self.assertIs(selector.choose([h],i/20),h)
        self.assertEqual(selector.label,'Right')

    def test_missing_label_does_not_discard_a_unique_continuous_wrist(self):
        selector=HandSelector();selector.choose([hand()],0)
        observed=hand(.51,.5,None,0)
        self.assertIs(selector.choose([observed],.05),observed)
        self.assertIsNone(selector.previous.label)

    def test_opposite_label_at_distant_position_is_never_accepted(self):
        for label in ('Left','Right',None):
            selector=HandSelector();selector.choose([hand(.1,.2)],0)
            for i in range(1,61):
                self.assertIsNone(selector.choose([hand(.9,.8,label)],i/20))
            self.assertAlmostEqual(selector.previous.x,.1)
            self.assertEqual(selector.last_reason,'association_position_jump')

    def test_far_second_hand_does_not_override_spatial_match(self):
        selector=HandSelector();target=hand(.2,.4,'Left',.6)
        selector.choose([target],0)
        for i in range(1,20):
            target=hand(.2+i*.002,.4,'Right' if i%2 else 'Left',.51)
            other=hand(.9,.8,'Left',1)
            candidates=[target,other] if i%2 else [other,target]
            self.assertIs(selector.choose(candidates,i/20),target)

    def test_known_other_hand_cannot_replace_missing_target(self):
        for delay in (.05,2.0):
            selector=HandSelector();a=hand(.3,.5);b=hand(.55,.5,'Left')
            selector.choose([a],0);selector.choose([a,b],.05)
            self.assertIsNone(selector.choose([b],.05+delay))
            self.assertTrue(selector.needs_reset)
            self.assertEqual(selector.last_reason,'possible_other_hand')
            self.assertIsNone(selector.choose([b],3))

    def test_two_hands_crossing_latches_before_order_reversal(self):
        for reverse_labels in (False,True):
            selector=HandSelector();selector.choose([hand(.2,.5)],0)
            stopped=False
            for i in range(1,24):
                x_a=.2+.02*i;x_b=.8-.02*i
                a=hand(x_a,.5,'Left' if reverse_labels and i>10 else 'Right')
                b=hand(x_b,.5,'Right' if reverse_labels and i>10 else 'Left')
                result=selector.choose([b,a] if i%2 else [a,b],i/20)
                if result is None:
                    stopped=True
                if not stopped:
                    self.assertIs(result,a)
                else:
                    self.assertIsNone(result)
                if x_a>=x_b:
                    self.assertTrue(stopped)
            self.assertTrue(selector.needs_reset)

    def test_close_or_equidistant_candidates_are_ambiguous_despite_labels(self):
        for candidates in ([hand(.48,.5),hand(.52,.5,'Left')],
                           [hand(.35,.5),hand(.65,.5,'Left')]):
            selector=HandSelector();selector.choose([hand()],0)
            self.assertIsNone(selector.choose(candidates,.05))
            self.assertTrue(selector.needs_reset)

    def test_unresolved_ambiguity_cannot_auto_resume_when_one_candidate_remains(self):
        selector=HandSelector();selector.choose([hand()],0)
        selector.choose([hand(.48,.5),hand(.52,.5,'Left')],.05)
        for i in range(2,20):
            self.assertIsNone(selector.choose([hand(.52,.5,'Left')],i/20))
        selector.reset()
        self.assertIsNotNone(selector.choose([hand(.52,.5,'Left')],1.1))

    def test_initial_two_hands_wait_for_unambiguous_choice(self):
        selector=HandSelector()
        self.assertIsNone(selector.choose([hand(.2,.4),hand(.8,.4,'Left')],0))
        self.assertEqual(selector.last_reason,'multiple_hands_waiting')
        self.assertFalse(selector.needs_reset)
        self.assertIsNotNone(selector.choose([hand(.8,.4,'Left')],.05))

    def test_initial_label_preference_is_auxiliary_and_never_overrides_overlap(self):
        selector=HandSelector('Left');left=hand(.8,.5,'Left');right=hand(.2,.5)
        self.assertIs(selector.choose([right,left],0),left)
        moved=hand(.79,.5,'Right')
        self.assertIs(selector.choose([moved,right],.05),moved)
        selector.reset()
        self.assertIsNone(selector.choose([hand(.5,.5,'Left'),hand(.51,.5)],1))

    def test_short_missing_observation_retains_anchor_and_accepts_near_return(self):
        selector=HandSelector();first=hand(.4,.4)
        selector.choose([first],0)
        self.assertIsNone(selector.choose([],.05))
        self.assertIs(selector.previous,first)
        returned=hand(.42,.4,'Left')
        self.assertIs(selector.choose([returned],.1),returned)

    def test_long_absence_requires_stable_return_near_anchor(self):
        selector=HandSelector();selector.choose([hand(.4,.4)],0)
        selector.choose([],.1)
        for i in range(5):
            self.assertIsNone(selector.choose([hand(.41,.4,'Left')],1+i*.05))
        returned=hand(.41,.4,'Left')
        self.assertIs(selector.choose([returned],1.3),returned)

    def test_broken_reacquisition_restarts_confirmation(self):
        selector=HandSelector();selector.choose([hand()],0)
        selector.choose([],1)
        selector.choose([hand()],1.05);selector.choose([hand()],1.1)
        selector.choose([],1.15)
        self.assertIsNone(selector.choose([hand()],1.2))
        self.assertIsNone(selector.choose([hand()],1.3))
        self.assertIsNone(selector.choose([hand()],1.4))
        self.assertIsNotNone(selector.choose([hand()],1.5))

    def test_new_ambiguity_during_reacquisition_never_returns_a_hand(self):
        selector=HandSelector();selector.choose([hand()],0);selector.choose([],1)
        selector.choose([hand()],1.05)
        self.assertIsNone(selector.choose([hand(.48,.5),hand(.52,.5,'Left')],1.1))
        self.assertTrue(selector.needs_reset)

    def test_reset_discards_anchor_competitors_ambiguity_and_label_hint(self):
        selector=HandSelector();selector.choose([hand()],0)
        selector.choose([hand(.49,.5),hand(.51,.5,'Left')],.05)
        selector.reset()
        self.assertFalse(selector.needs_reset)
        self.assertIsNone(selector.previous)
        self.assertEqual(selector.others,[])
        self.assertIsNone(selector.label)
        distant=hand(.9,.1,'Left')
        self.assertIs(selector.choose([distant],.1),distant)

    def test_duplicate_or_old_samples_do_not_mutate_track(self):
        selector=HandSelector();a=hand();selector.choose([a],1)
        self.assertIsNone(selector.choose([hand(.9,.9,'Left')],1))
        self.assertIsNone(selector.choose([],0.9))
        self.assertIs(selector.previous,a)

    def test_label_changes_do_not_interrupt_60_to_90_bpm(self):
        for fps in (18,20,24):
            engine=Conductor();selector=HandSelector()
            for i in range(24*fps):
                t=i/fps;phase=t if t<10 else 10+1.5*(t-10)
                h=hand(.5,.51+.28*math.cos(2*math.pi*phase),'Left' if i%3 else 'Right')
                chosen=selector.choose([h],t)
                self.assertIs(chosen,h)
                engine.update(t,h.x,h.y)
                if i==9*fps:
                    self.assertAlmostEqual(engine.bpm,60,delta=.3)
            self.assertAlmostEqual(engine.bpm,90,delta=.3)
            self.assertEqual(engine.diagnostic_counts['tracking_lost:no_hand'],0)

    def test_disappearance_and_reentry_never_bridge_a_beat_interval(self):
        engine=Conductor();selector=HandSelector();events=[];recovery=None
        for i in range(300):
            t=i/20;absent=8<=t<9
            candidates=[] if absent else [hand(.5,.51+.28*math.cos(2*math.pi*t),'Left' if t>=9 else 'Right')]
            h=selector.choose(candidates,t)
            event=engine.update(t,h.x if h else None,h.y if h else None,missing_reason=selector.last_reason)
            if t>=9 and h and recovery is None:
                recovery=t
                self.assertIsNone(event)
            if event:events.append(event)
        after=[e for e in events if e.t>=9]
        self.assertTrue(after)
        self.assertIsNone(after[0].interval)
        self.assertTrue(all(abs(e.interval-1)<.02 for e in after[1:] if e.interval is not None))
        self.assertAlmostEqual(engine.bpm,60,delta=.3)

    def test_ambiguity_outputs_missing_samples_not_beats(self):
        engine=Conductor();selector=HandSelector()
        for i in range(160):
            t=i/20;h=selector.choose([hand(.5,.51+.28*math.cos(2*math.pi*t))],t)
            engine.update(t,h.x,h.y)
        count=engine.count
        for i in range(160,190):
            t=i/20
            h=selector.choose([hand(.48,.75),hand(.52,.75,'Left')],t)
            self.assertIsNone(h)
            self.assertIsNone(engine.update(t,None,None,missing_reason=selector.last_reason))
        self.assertEqual(engine.count,count)
        self.assertIsNone(engine.output_bpm)

    def test_diagnostics_are_counts_distances_and_labels_not_coordinates(self):
        selector=HandSelector();selector.choose([hand()],0)
        selector.choose([hand(.51,.52,'Left')],.05)
        self.assertTrue(selector.last_details['model_label_changed'])
        self.assertEqual(selector.last_details['association_state'],'tracking')
        self.assertFalse(set(selector.last_details)&{'x','y','landmarks','image','frame'})

    def test_association_configuration_validation(self):
        for values in ({'ambiguity_radius':0},{'distance_margin':2},{'reacquire_s':float('nan')},
                       {'distractor_memory_s':False}):
            with self.assertRaises(ValueError):
                AssociationConfig(**values)

    def test_merged_detection_between_two_previous_hands_is_ambiguous(self):
        selector=HandSelector();a=hand(.3,.5);b=hand(.6,.5,'Left')
        selector.choose([a],0);selector.choose([a,b],.05)
        self.assertIsNone(selector.choose([hand(.45,.5)],.1))
        self.assertTrue(selector.needs_reset)

    def test_invalid_target_plus_other_valid_hand_cannot_switch_identity(self):
        selector=HandSelector();selector.choose([hand(.4,.5)],0)
        self.assertIsNone(selector.choose([hand(.4,float('nan')),hand(.5,.5,'Left')],.05))
        self.assertTrue(selector.needs_reset)
        self.assertEqual(selector.last_reason,'invalid_competing_detection')

    def test_initial_invalid_competitor_waits_without_latching_an_identity(self):
        selector=HandSelector()
        self.assertIsNone(selector.choose([hand(.5,1.01),hand(.6,.5,'Left')],0))
        self.assertIsNone(selector.previous)
        self.assertFalse(selector.needs_reset)
        self.assertIsNotNone(selector.choose([hand(.6,.5,'Left')],.05))

    def test_old_distractor_is_forgotten_during_continuous_unambiguous_tracking(self):
        selector=HandSelector();a=hand(.2,.5);b=hand(.8,.5,'Left')
        selector.choose([a],0);selector.choose([a,b],.05)
        for i in range(2,20):
            self.assertIs(selector.choose([a],i/20),a)
        self.assertEqual(selector.others,[])


if __name__=='__main__':
    unittest.main()
