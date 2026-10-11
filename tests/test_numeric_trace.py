"""Local diagnostic output separates raw-model results from routing decisions."""
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest

from conductor.multitrack import ConductorSession
from conductor.numeric_trace import NumericTrace,frame_record,result_counts
from tests.test_tip_anchor import observation,drive,moving


def result(*hands):
    return SimpleNamespace(hand_landmarks=[h.landmarks for h in hands],handedness=[])


def record(s,r,accepted=True,completed=8.02,timestamp=8):
    return frame_record(s,start=0,timestamp=timestamp,completed_at=completed,sequence=100,
        synthetic=False,accepted=accepted,inference_ms=10,preparation_ms=2,
        association_ms=1,processing_ms=13,frame_age_at_start_ms=7,counts=result_counts(r))


class NumericTraceTests(unittest.TestCase):
    def stable(self):
        s=ConductorSession()
        drive(s,0,8,lambda t:[moving(t)])
        return s

    def test_two_raw_candidates_one_retained_main_is_explicit(self):
        s=self.stable()
        r=result(moving(8),observation(x=.30))
        accepted,_=s.consume_result(r,8,8.02)
        row=record(s,r,accepted)
        self.assertTrue(row['accepted'])
        self.assertEqual(row['model_candidate_count'],2)
        self.assertEqual(row['routing_after']['visible_tracks'],1)
        self.assertEqual(row['association']['suppressed_candidate_indices'],[1])
        self.assertAlmostEqual(row['result_age_ms'],20)
        self.assertEqual(row['inference_ms'],10)
        json.dumps(row,allow_nan=False)

    def test_stale_model_output_not_misreported_as_empty_model_result(self):
        s=self.stable()
        r=result(moving(8),observation(x=.30))
        accepted,_=s.consume_result(r,8,8.3)
        row=record(s,r,accepted,completed=8.3)
        self.assertFalse(row['accepted'])
        self.assertEqual(row['result_status'],'inference_result_too_old')
        self.assertEqual(row['model_candidate_count'],2)
        self.assertIsNone(row['association'])
        self.assertIsNone(row['routing_after']['primary_track'])

    def test_duplicate_result_logs_rejection_without_reusing_old_decision(self):
        s=self.stable()
        r=result(moving(8))
        s.consume_result(r,8,8.01)
        accepted,_=s.consume_result(r,8,8.02)
        row=record(s,r,accepted)
        self.assertEqual(row['result_status'],'nonmonotonic_or_invalid_result')
        self.assertIsNone(row['association'])
        self.assertEqual(row['routing_after']['primary_track'],'A')

    def test_empty_output_is_distinct_from_ambiguous_output(self):
        s=self.stable()
        r=result()
        accepted,_=s.consume_result(r,8,8.01)
        row=record(s,r,accepted)
        self.assertEqual(row['model_candidate_count'],0)
        self.assertEqual(row['association']['decision'],'no_hands')
        self.assertFalse(row['association']['wrist_distances'][0])

    def test_invalid_wrist_and_tip_are_counts_not_nonfinite_coordinates(self):
        s=self.stable()
        h=observation(x=float('nan'))
        r=result(h)
        accepted,_=s.consume_result(r,8,8.01)
        row=record(s,r,accepted)
        self.assertEqual(row['model_valid_wrists'],0)
        self.assertEqual(row['model_valid_tips'],0)
        json.dumps(row,allow_nan=False)
        def inspect(value):
            if isinstance(value,dict):
                self.assertFalse(set(value)&{'x','y','landmarks','hand_landmarks','image','pixels','frame'})
                for child in value.values():
                    inspect(child)
            elif isinstance(value,list):
                for child in value:
                    inspect(child)
        inspect(row)

    def test_trace_flushes_complete_json_records_and_closes(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'frames.jsonl'
            trace=NumericTrace(path)
            for i in range(31):
                trace.write({'sequence':i,'inference_ms':10.5})
            self.assertGreaterEqual(len(path.read_text().splitlines()),30)
            trace.close()
            rows=[json.loads(x) for x in path.read_text().splitlines()]
            self.assertEqual([x['sequence'] for x in rows],list(range(31)))
            self.assertTrue(trace.stream.closed)

    def test_nonfinite_values_are_rejected_without_writing_invalid_json(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'frames.jsonl'
            trace=NumericTrace(path)
            with self.assertRaises(ValueError):
                trace.write({'inference_ms':float('nan')})
            trace.close()
            self.assertEqual(path.read_text(),'')
