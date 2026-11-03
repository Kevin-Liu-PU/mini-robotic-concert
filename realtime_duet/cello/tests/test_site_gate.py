"""Bundled gate denies the shipped unapproved templates; no robot imports."""
import json
from pathlib import Path
import tempfile
import time
import unittest
from cello_adapter.hardware import digest
from cello_adapter.preflight_check import inspect
from cello_adapter.site_gate import REQUIRED_CHECKS, validate_session

ROOT = Path(__file__).resolve().parents[1]


class SiteGateTests(unittest.TestCase):
    def test_shipped_template_is_explicitly_blocked(self):
        report = inspect(ROOT / "configs/hardware-review.UNAPPROVED.json")
        self.assertEqual(report["status"], "BLOCKED")
        self.assertFalse(report["robot_connected"])
        self.assertIn("review_status", [x["field"] for x in report["issues"]])
        self.assertNotIn("gate_path.validate_session", [x["field"] for x in report["issues"]])
        self.assertNotIn("gate_path.validate_connected_session", [x["field"] for x in report["issues"]])

    def test_checklist_requires_all_evidence_and_fresh_identity(self):
        # Temporary unit fixture is not written into a production configuration.
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "TEST_ONLY_FAKE_CHECKLIST.json"
            data = dict(status="OPERATOR_REVIEWED_FOR_THIS_SESSION", operator="TEST_FIXTURE_NOT_A_REAL_OPERATOR",
                        session_id="FAKE", clock_domain="FAKE", mode="air", reviewed_unix_s=time.time()-1,
                        expires_unix_s=time.time()+60,
                        site_rules="none_identified", checks={k:dict(verified=True) for k in REQUIRED_CHECKS})
            review = dict(onsite_checklist_path=str(path), mode="air", session_id="FAKE", clock_domain="FAKE")
            def save():
                path.write_text(json.dumps(data))
                review["pins"] = {str(path.resolve()): digest(path)}
            save()
            self.assertTrue(validate_session(review, "FAKE", "FAKE"))
            data["checks"][REQUIRED_CHECKS[0]]["verified"] = False
            save()
            with self.assertRaisesRegex(ValueError, "confirmation required"):
                validate_session(review, "FAKE", "FAKE")
            data["checks"][REQUIRED_CHECKS[0]]["verified"] = True
            data["session_id"] = "OTHER"
            save()
            with self.assertRaisesRegex(ValueError, "another session"):
                validate_session(review, "FAKE", "FAKE")

    def test_checklist_hash_is_enforced(self):
        path = ROOT / "configs/onsite-checklist.UNAPPROVED.json"
        with self.assertRaisesRegex(ValueError, "pins"):
            validate_session(dict(onsite_checklist_path=str(path), pins={}, session_id="FAKE", clock_domain="FAKE"), "FAKE", "FAKE")
