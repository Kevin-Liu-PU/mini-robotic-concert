"""Geometry equivalence and forbidden-import tests. No real driver calls."""
import ast
import builtins
import math
from pathlib import Path
import sys
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from cello_adapter.review_validation import PACKAGES, UPSTREAM_RUNTIME, calibration_data
from test_phase_servo import fake_limits

try:
    import numpy as np
except ImportError:
    np = None

ROOT = Path(__file__).resolve().parents[1]


def synthetic_data():
    # Synthetic, dimensionless test path, never a robot calibration.
    return dict(BOW_CONFIGS={"standard": dict(frog=[0,0,0,0,0,0], tip=[1,0,0,0,0,0],
                fsafe=[0,0,.1,0,0,0], tsafe=[1,0,.1,0,0,0])}, CONFIG_NAME="standard",
                U_MIN=0, U_MAX=1, MAX_OUTWARD_DEPTH=.1, MAX_INWARD_DEPTH=.1,
                ACCEL_MAX=20, CALIBRATED_SPEED_MAX=2, VERIFIED_TCP=[0]*6,
                VERIFIED_PAYLOAD_KG=.1, VERIFIED_PAYLOAD_COG=[0]*3)


@unittest.skipIf(np is None, "NumPy geometry tests require the actual minimal math dependency")
class PureGeometryTests(unittest.TestCase):
    def test_hardware_entry_uses_only_numpy_and_fake_rtde_no_audio_or_midi_import(self):
        from cello_adapter.hardware import open_hardware
        from cello_adapter.geometry import CalibratedGeometry
        from cello_adapter import site_gate
        data = synthetic_data()
        geometry = CalibratedGeometry(data)
        calls, imports = [], []
        class FakeDriver:
            def __init__(self, address): calls.append("fake_connect")
            def isConnected(self): return True
            def isProgramRunning(self): return True
            def getTCPOffset(self): return data["VERIFIED_TCP"]
            def getPayload(self): return data["VERIFIED_PAYLOAD_KG"]
            def getPayloadCog(self): return data["VERIFIED_PAYLOAD_COG"]
            def getTimestamp(self): return 1.0
            def getSafetyMode(self): return 1
            def getActualTCPForce(self): return [0]*6
            def getActualTCPPose(self): return geometry.pose_at(.2)
            def servoStop(self): calls.append("fake_stop"); return True
            def disconnect(self): calls.append("fake_disconnect")
        original_import = builtins.__import__
        def guarded_import(name, *args, **kwargs):
            imports.append(name)
            if name.split(".")[0] in {"mido", "sounddevice", "soundfile", "BaselineControls", "recording_a_only"}:
                raise AssertionError("forbidden broad import: " + name)
            return original_import(name, *args, **kwargs)
        review = dict(robot_ip="TEST_ONLY_NO_SOCKET", allowed_safety_modes=[1], expires_unix_s=time.time()+10)
        fake_modules = dict(rtde_control=SimpleNamespace(RTDEControlInterface=FakeDriver),
                            rtde_receive=SimpleNamespace(RTDEReceiveInterface=FakeDriver))
        with patch("cello_adapter.hardware.preflight", return_value=(review, fake_limits())), \
             patch("cello_adapter.review_validation.calibration_data", return_value=data), \
             patch.object(site_gate, "validate_session", return_value=True), \
             patch.object(site_gate, "validate_connected_session", return_value=True), \
             patch.dict(sys.modules, fake_modules), patch("builtins.__import__", side_effect=guarded_import):
            backend = open_hardware("TEST_ONLY", "TEST_ONLY", "TEST", "TEST",
                                    execute=True, attended=True, armed=True)
            self.assertEqual(backend.close(), "driver_stop_returned_unverified")
        self.assertEqual(calls.count("fake_connect"), 2)
        self.assertEqual(calls.count("fake_stop"), 1)
        self.assertEqual(calls.count("fake_disconnect"), 2)
        self.assertIn("rtde_control", imports)
        self.assertIn("rtde_receive", imports)

    def test_dependency_inventory_excludes_unused_controller_audio_and_midi(self):
        self.assertEqual(set(PACKAGES), {"numpy", "ur-rtde"})
        self.assertNotIn("BaselineControls/baseline_controller.py", UPSTREAM_RUNTIME)

    def test_synthetic_path_depth_rotation_and_checks(self):
        from cello_adapter.geometry import CalibratedGeometry
        g = CalibratedGeometry(synthetic_data())
        g._verify_geometry()
        np.testing.assert_array_equal(g.pose_at(.2), [.2,0,0,0,0,0])
        np.testing.assert_allclose(g.apply_depth(g.pose_at(.2), g.CFG, .01), [.2,0,-.01,0,0,0])
        np.testing.assert_array_equal(g._rv_to_matrix([0,0,0]), np.eye(3))
        data = synthetic_data()
        data["BOW_CONFIGS"]["standard"]["tip"][3] = .5
        with self.assertRaisesRegex(ValueError, "orientation span"):
            CalibratedGeometry(data)._verify_geometry()

    @unittest.skipUnless((ROOT / "robotic-cellist/BaselineControls/play_midi_pieces.py").exists(),
                         "Upstream source equivalence is development evidence; no device calibration is bundled")
    def test_deterministic_equivalence_to_reviewed_upstream_pure_functions(self):
        from cello_adapter.geometry import CalibratedGeometry
        repo = ROOT / "robotic-cellist"
        data = calibration_data(dict(repo_root=str(repo)))
        g = CalibratedGeometry(data)
        # Compile ONLY the five statically inspected pure function definitions,
        # never imports, module initialization, motion, or calibration scripts.
        selected = {
            "BaselineControls/play_midi_pieces.py": {"_rv_to_matrix", "_canonicalise_rotvec", "pose_at"},
            "SoundClassifier/Data_Collection/recording_a_only.py": {"press_direction", "apply_depth"}}
        namespace = dict(np=np, math=math, FROG=g.FROG, TIP=g.TIP,
                         MAX_OUTWARD_DEPTH=g.MAX_OUTWARD_DEPTH, MAX_INWARD_DEPTH=g.MAX_INWARD_DEPTH)
        for path, names in selected.items():
            tree = ast.parse((repo / path).read_text(encoding="utf-8"))
            functions = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name in names]
            self.assertEqual({f.name for f in functions}, names)
            self.assertFalse(any(isinstance(n, (ast.Import, ast.ImportFrom)) for f in functions for n in ast.walk(f)))
            exec(compile(ast.Module(body=functions, type_ignores=[]), str(repo/path), "exec"), namespace)
        namespace["_TIP_ROT_CANON"] = namespace["_canonicalise_rotvec"](g.TIP[3:], g.FROG[3:])
        for u in [-1,0,1e-10,1e-8,*np.linspace(.01,.99,99),1-1e-10,1,2]:
            np.testing.assert_array_equal(g.pose_at(u), namespace["pose_at"](u))
            for depth in [-.01,-.0015,0,.0008,.002,.01]:
                np.testing.assert_array_equal(g.apply_depth(g.pose_at(u), g.CFG, depth),
                                               namespace["apply_depth"](g.pose_at(u), g.CFG, depth))
        for rotation in ([0,0,0], [1e-14,0,0], g.FROG[3:], g.TIP[3:], [math.pi,0,0], [1,2,3]):
            np.testing.assert_array_equal(g._rv_to_matrix(rotation), namespace["_rv_to_matrix"](rotation))
        np.testing.assert_array_equal(g.press_direction(g.CFG), namespace["press_direction"](g.CFG))
        g._verify_geometry()


if __name__ == "__main__": unittest.main()
