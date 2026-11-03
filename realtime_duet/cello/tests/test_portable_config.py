"""Portable paths/candidate tooling only; no device modules or network."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from cello_adapter.review_validation import resolve_review_paths, validate_review, digest
from tools.prepare_candidate_inventory import prepare


class PortableConfigurationTests(unittest.TestCase):
    def test_paths_and_pins_resolve_from_manifest_without_mutating_input(self):
        with tempfile.TemporaryDirectory() as folder:
            base = Path(folder) / "config with spaces"
            source = dict(repo_root="../existing cello", gate_path="../adapter/site_gate.py",
                          onsite_checklist_path="onsite.json", operating_profile_path=None,
                          pins={"../existing cello/a.py": "abc"})
            result = resolve_review_paths(source, base)
            self.assertEqual(result["repo_root"], str((base / "../existing cello").resolve()))
            self.assertEqual(result["onsite_checklist_path"], str((base / "onsite.json").resolve()))
            self.assertEqual(result["pins"], {str((base / "../existing cello/a.py").resolve()): "abc"})
            self.assertEqual(source["repo_root"], "../existing cello")
            self.assertIsNone(result["operating_profile_path"])

    def test_shared_validator_resolves_relative_extra_pin_and_detects_change(self):
        with tempfile.TemporaryDirectory() as folder:
            base = Path(folder)
            item = base / "data.json"
            item.write_text("{}")
            review = base / "review.json"
            review.write_text(json.dumps(dict(repo_root="repo", pins={"data.json": digest(item)})))
            resolved, _, report = validate_review(review)
            self.assertEqual(resolved["repo_root"], str(base / "repo"))
            self.assertFalse(any(x["field"].endswith("data.json") for x in report["issues"]))
            item.write_text('{"changed":true}')
            _, _, report = validate_review(review)
            self.assertTrue(any(x["field"].endswith("data.json") for x in report["issues"]))

    def test_candidate_uses_selected_local_paths_and_never_approves(self):
        with tempfile.TemporaryDirectory() as folder:
            base = Path(folder)
            repo, out = base / "existing repo", base / "local config"
            data = dict(bow_length_m=.5, SERVO_DT=.008, SERVO_LOOKAHEAD=.1, SERVO_GAIN=300)
            # Only source parsing is substituted; generated records are all
            # unapproved and no physical limits beyond derivation are supplied.
            with patch("tools.prepare_candidate_inventory.calibration_data", return_value=data), \
                 patch("tools.prepare_candidate_inventory.required_paths", return_value=set()):
                result = prepare(repo, out, session="TEST", domain="TEST")
            candidate = json.loads(Path(result["candidate"]).read_text())
            self.assertEqual(candidate["repo_root"], str(repo))
            self.assertEqual(candidate["review_status"], "UNAPPROVED")
            self.assertFalse(candidate["open_a_rhythm_approved"])
            self.assertIsNone(candidate["limits"]["force_max_n"])
            record = json.loads(Path(candidate["onsite_checklist_path"]).read_text())
            self.assertTrue(all(v["verified"] is False for v in record["checks"].values()))

    def test_candidate_refuses_to_overwrite_a_reviewed_record(self):
        with tempfile.TemporaryDirectory() as folder:
            out = Path(folder)
            record = out / "onsite-checklist.CANDIDATE-UNAPPROVED.json"
            original = '{"status":"OPERATOR_REVIEWED_FOR_THIS_SESSION"}'
            record.write_text(original)
            with self.assertRaisesRegex(ValueError, "refusing to overwrite"):
                prepare(out / "repo", out)
            self.assertEqual(record.read_text(), original)


if __name__ == "__main__": unittest.main()
