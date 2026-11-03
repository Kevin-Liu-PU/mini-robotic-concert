"""Synthetic preflight fixtures only. No usable hardware approvals generated."""
from dataclasses import asdict
import json
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch
from cello_adapter.hardware import digest, preflight, open_hardware, UpstreamPort
from cello_adapter.phase_servo import ServoFault
from test_phase_servo import fake_limits
from cello_adapter.review_validation import REQUIRED_CHECKS, PACKAGES, required_paths


class Vec(list):
    def __getitem__(self, key):
        value = super().__getitem__(key)
        return Vec(value) if isinstance(key, slice) else value
    def __add__(self, other):
        return Vec(a+b for a,b in zip(self,other))
    def __mul__(self, scale):
        return Vec(a*scale for a in self)
    __rmul__ = __mul__


class IdentityRotation:
    @property
    def T(self):
        return self
    def __matmul__(self, other):
        return self
    def trace(self):
        return 3


class FakeGeometry:
    U_MIN, U_MAX, CFG = 0, 1, {}
    @staticmethod
    def pose_at(u):
        return Vec([u, 0, 0, 0, 0, 0])
    @staticmethod
    def apply_depth(pose, cfg, depth):
        return Vec(pose)
    @staticmethod
    def press_direction(cfg):
        return Vec([0, 0, 1])
    @staticmethod
    def _rv_to_matrix(rotation):
        return IdentityRotation()


class FakeRTDE:
    def __init__(self):
        self.running = True
        self.mode = 1
        self.force = [0]*6
        self.pose = [.2, 0, 0, 0, 0, 0]
        self.stop_result = True
    def isConnected(self): return True
    def isProgramRunning(self): return self.running
    def getSafetyMode(self): return self.mode
    def getActualTCPForce(self): return self.force
    def getActualTCPPose(self): return self.pose
    def getTimestamp(self): return 1.0
    def servoStop(self): return self.stop_result


class PortSafetyTests(unittest.TestCase):
    def setUp(self):
        self.io = FakeRTDE()
        self.port = UpstreamPort(self.io, self.io, FakeGeometry, fake_limits(), [1])

    def test_valid_calibrated_pose(self):
        self.port.check_safety(.2)
        self.assertEqual(self.port.pose(.3), [.3, 0, 0, 0, 0, 0])

    def test_force_and_torque_fail_closed(self):
        for axis in (0, 3):
            self.io.force = [0]*6
            self.io.force[axis] = 2
            with self.assertRaises(ServoFault):
                self.port.check_safety(.2)

    def test_actual_pose_is_checked_no_automatic_positioning(self):
        self.io.pose[0] = .5
        with self.assertRaisesRegex(ServoFault, "no automatic positioning"):
            self.port.check_safety(.2)

    def test_control_or_safety_fault(self):
        self.io.running = False
        with self.assertRaises(ServoFault): self.port.check_safety(.2)
        self.io.running = True
        self.io.mode = 3
        with self.assertRaises(ServoFault): self.port.check_safety(.2)

    def test_invalid_measurement_and_stop_failure(self):
        self.io.force[0] = float("nan")
        with self.assertRaises(ServoFault): self.port.check_safety(.2)
        self.io.stop_result = False
        with self.assertRaisesRegex(ServoFault, "servoStop"):
            self.port.stop()

    def test_stale_robot_telemetry_and_expiration_fail(self):
        with patch("cello_adapter.hardware.time.perf_counter_ns", return_value=0):
            self.port.check_safety(.2)
        with patch("cello_adapter.hardware.time.perf_counter_ns", return_value=200000000):
            with self.assertRaisesRegex(ServoFault, "stale"):
                self.port.check_safety(.2)
        self.port.expires_unix_s = time.time()-1
        with self.assertRaisesRegex(ServoFault, "expired"):
            self.port.check_safety(.2)


class ReviewTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        repo = root / "FAKE_REPO_NEVER_IMPORTED"
        sources = {
            "BaselineControls/play_midi_pieces.py": "CONFIG_NAME='standard'\nU_MIN=0\nU_MAX=1\nCALIBRATED_SPEED_MAX=2\nACCEL_MAX=20\n",
            "SoundClassifier/Data_Collection/recording_a_only.py": "BOW_CONFIGS={'standard':{'frog':np.array([0,0,0,0,0,0]),'tip':np.array([1,0,0,0,0,0]),'fsafe':np.array([0,0,.1,0,0,0]),'tsafe':np.array([1,0,.1,0,0,0])}}\nMAX_OUTWARD_DEPTH=.1\nMAX_INWARD_DEPTH=.1\nVERIFIED_TCP=[0,0,0,0,0,0]\nVERIFIED_PAYLOAD_KG=.1\nVERIFIED_PAYLOAD_COG=[0,0,0]\n",
            "BaselineControls/baseline_controller.py": "",
            "BaselineControls/servo_player.py": "SERVO_DT=.01\nSERVO_LOOKAHEAD=.1\nSERVO_GAIN=100\n"}
        for rel, data in sources.items():
            f = repo / rel
            f.parent.mkdir(parents=True, exist_ok=True)
            f.write_text("raise RuntimeError('TEST FIXTURE: must never import')\n" + data)
        import cello_adapter.hardware as hw
        gate = Path(hw.__file__).with_name("site_gate.py")
        self.checklist = root / "TEST_ONLY_FAKE_RECORD.json"
        self.checklist.write_text(json.dumps(dict(status="OPERATOR_REVIEWED_FOR_THIS_SESSION", operator="TEST_ONLY",
            session_id="fake", mode="contact", site_rules="none_identified",
            checks={k:{"verified":True} for k in REQUIRED_CHECKS})))
        versions = {"python":"3.11.0", **{p:"TEST_ONLY" for p in PACKAGES}}
        self.review = dict(review_status="REVIEWED_FOR_ONSITE_TEST", session_id="fake", clock_domain="fake", expires_unix_s=time.time()+60,
                           open_a_rhythm_approved=True, mode="contact", robot_ip="127.0.0.1",
                           repo_root=str(repo), gate_path=str(gate), limits=asdict(fake_limits()),
                           allowed_safety_modes=[1], environment={}, onsite_checklist_path=str(self.checklist),
                           runtime_versions=dict(versions))
        self.review["pins"] = {str(p):digest(p) for p in required_paths(self.review)}
        self.path = root / "FAKE_REVIEW_NOT_FOR_EXECUTION.json"
        self.env = patch.dict(os.environ, {k:v for k,v in os.environ.items() if not k.startswith("CELLO_")}, clear=True)
        self.env.start()
        self.versions = patch("cello_adapter.review_validation.dependency_versions", return_value=versions)
        self.versions.start()

    def tearDown(self):
        self.env.stop()
        self.versions.stop()
        self.tmp.cleanup()

    def write(self):
        self.path.write_text(json.dumps(self.review))
        return digest(self.path)

    def test_static_preflight_imports_no_gate_or_driver(self):
        # Fake gate/geometry raise if imported; preflight must still succeed.
        preflight(self.path, self.write(), "fake", "fake")

    def test_changed_review_hash_rejected(self):
        self.write()
        with self.assertRaisesRegex(ValueError, "hash"):
            preflight(self.path, "0"*64, "fake", "fake")

    def test_expired_and_wrong_session_rejected(self):
        with self.assertRaisesRegex(ValueError, "session/domain"):
            preflight(self.path, self.write(), "other", "fake")
        self.review["expires_unix_s"] = time.time()-1
        with self.assertRaisesRegex(ValueError, "expired"):
            preflight(self.path, self.write(), "fake", "fake")

    def test_missing_source_pin_and_limit_rejected(self):
        self.review["pins"].pop(next(iter(self.review["pins"])))
        with self.assertRaisesRegex(ValueError, "missing or changed"):
            preflight(self.path, self.write(), "fake", "fake")
        del self.review["limits"]["force_max_n"]
        with self.assertRaisesRegex(ValueError, "force_max_n"):
            preflight(self.path, self.write(), "fake", "fake")

    def test_cannot_broaden_safety_modes(self):
        self.review["allowed_safety_modes"] = [3]
        with self.assertRaisesRegex(ValueError, "broaden"):
            preflight(self.path, self.write(), "fake", "fake")

    def test_execution_flags_required_before_any_import(self):
        with self.assertRaisesRegex(ValueError, "execute"):
            open_hardware("does-not-exist", "", "fake", "fake")

    def test_gate_must_actually_approve_before_hardware_imports(self):
        data = json.loads(self.checklist.read_text())
        data["status"] = "UNAPPROVED"
        self.checklist.write_text(json.dumps(data))
        self.review["pins"][str(self.checklist.resolve())] = digest(self.checklist)
        with self.assertRaisesRegex(ValueError, "UNAPPROVED"):
            open_hardware(self.path, self.write(), "fake", "fake",
                          execute=True, attended=True, armed=True)

    def test_unrelated_rl_analysis_change_does_not_block_baseline(self):
        path = Path(self.review["repo_root"])/"rl/unrelated_analysis.py"
        path.parent.mkdir()
        path.write_text("raise RuntimeError('UNRELATED NOT EXECUTED')")
        preflight(self.path, self.write(), "fake", "fake")
        path.write_text("raise RuntimeError('CHANGED UNRELATED')")
        preflight(self.path, self.write(), "fake", "fake")

    def test_actual_runtime_dependency_change_is_blocked(self):
        path = Path(self.review["repo_root"])/"BaselineControls/play_midi_pieces.py"
        path.write_text(path.read_text()+"\n# changed actual dependency\n")
        with self.assertRaisesRegex(ValueError, "identities missing or changed"):
            preflight(self.path, self.write(), "fake", "fake")

    def test_report_and_runtime_use_identical_validator(self):
        from cello_adapter.preflight_check import inspect
        sha = self.write()
        self.assertEqual(inspect(self.path, sha)["issues"], [])
        preflight(self.path, sha, "fake", "fake")
        data = json.loads(self.checklist.read_text())
        data["operator"] = ""
        self.checklist.write_text(json.dumps(data))
        self.review["pins"][str(self.checklist.resolve())] = digest(self.checklist)
        sha = self.write()
        self.assertEqual(inspect(self.path, sha)["issues"][0]["field"], "onsite_checklist.operator")
        with self.assertRaisesRegex(ValueError, "onsite_checklist.operator"):
            preflight(self.path, sha, "fake", "fake")

    def test_derive_bow_length_and_existing_servo_profile_without_import(self):
        for field in ("bow_length_m", "period_s", "lookahead_s", "gain"):
            self.review["limits"][field] = None
        _, limits = preflight(self.path, self.write(), "fake", "fake")
        self.assertEqual((limits.bow_length_m, limits.period_s, limits.lookahead_s, limits.gain), (1,.01,.1,100))

    def test_dependency_version_mismatch_is_visible_to_preflight(self):
        self.review["runtime_versions"]["ur-rtde"] = "WRONG_TEST_VERSION"
        with self.assertRaisesRegex(ValueError, "runtime_versions"):
            preflight(self.path, self.write(), "fake", "fake")


class InstallationRoundoffTests(unittest.TestCase):
    def test_serialization_roundoff_allowed_but_real_offset_rejected(self):
        from cello_adapter.review_validation import installation_equal
        self.assertTrue(installation_equal([1e-12, .1+1e-12], [0, .1]))
        self.assertFalse(installation_equal([1e-6, .1], [0, .1]))
        self.assertFalse(installation_equal([float("nan")], [0]))
        self.assertFalse(installation_equal([0], [0,0]))

    def test_numeric_tolerance_boundary(self):
        from cello_adapter.review_validation import installation_equal, INSTALLATION_ROUNDOFF_ABS
        self.assertTrue(installation_equal([INSTALLATION_ROUNDOFF_ABS], [0]))
        self.assertFalse(installation_equal([INSTALLATION_ROUNDOFF_ABS*1.001], [0]))


if __name__ == "__main__":
    unittest.main()
