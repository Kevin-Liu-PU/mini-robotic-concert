"""Synthetic scalar association contracts, not camera trajectory evidence."""
from collections import deque
import json
from pathlib import Path
import unittest
from unittest.mock import patch

from conductor.multitrack import MultiHandTracker, Track
from tests.test_tip_anchor import observation

ROOT = Path(__file__).resolve().parents[1]
from tests.support.synthetic_fixtures import scalar_cases
FIXTURE = scalar_cases()


def replay_scalar_decision(case):
    frame = case['record']['frame']
    slot, generation = case['record']['before_main']
    old_hand = observation()
    candidates = [observation() for _ in range(frame['model_candidates'])]
    memory_hand = observation() if frame['competitor_distances'] else None
    tracker = MultiHandTracker()
    tracker.last_input = 1-frame['input_dt_s']
    tracker.serial = generation
    # Three placeholders encode only the specified recent-motion maximum.
    # These values describe a contract case, not a reconstructed trajectory.
    recent_max = frame['recent_max_step'] or 0
    tracker.tracks[slot] = Track(slot, generation, old_hand, 0, tracker.last_input,
        deque(maxlen=7), False, deque([recent_max]*3, maxlen=7))
    if memory_hand:
        tracker.competitor = memory_hand, 1-frame['competitor_age_s']

    def distance(a, b):
        for i, candidate in enumerate(candidates):
            if a is old_hand and b is candidate:
                return frame['wrist_distances'][0][i]
            if a is memory_hand and b is candidate:
                return frame['competitor_distances'][i]
        if len(candidates)==2 and a is candidates[0] and b is candidates[1]:
            return frame['pair_distance']
        raise AssertionError('Unspecified distance requested by scalar replay')

    with patch.object(tracker, 'distance', side_effect=distance), patch(
            'conductor.multitrack.displacement_limit', return_value=frame['wrist_limits'][0]):
        tracker.update(candidates, 1, (slot,generation))
    retained = slot in tracker.tracks and tracker.tracks[slot].token==(slot,generation)
    return retained, tracker.last_decision


class SyntheticAssociationDecisions(unittest.TestCase):
    def test_synthetic_generation_is_deterministic_and_scope_is_explicit(self):
        self.assertEqual(FIXTURE['source'], 'HAND_AUTHORED_SYNTHETIC')
        self.assertEqual(FIXTURE, scalar_cases())
        self.assertEqual(len(FIXTURE['cases']),9)
        self.assertIn('NOT a recorded trajectory replay',FIXTURE['evidence_scope'])
        self.assertEqual(sum(c['expected_retain_main'] for c in FIXTURE['cases']),6)


def _make_test(case):
    def test(self):
        retained, decision = replay_scalar_decision(case)
        self.assertEqual(retained, case['expected_retain_main'], decision)
        if retained:
            # All six positive synthetic cases identify candidate 0, not just the token.
            self.assertEqual(decision['assigned_candidates'],{case['record']['before_main'][0]:0})
    return test


for _case in FIXTURE['cases']:
    setattr(SyntheticAssociationDecisions, 'test_'+_case['id'], _make_test(_case))


if __name__ == '__main__':
    unittest.main()
