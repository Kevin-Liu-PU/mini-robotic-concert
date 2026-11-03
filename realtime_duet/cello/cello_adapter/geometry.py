"""Pure NumPy geometry; no device imports, source execution or coordinates.

Formulas adapted from robotic-cellist commit 9468eddc0b8fb6d43fa7a1dad7a269dc7e0ad59a:
BaselineControls/play_midi_pieces.py (_rv_to_matrix, _canonicalise_rotvec,
pose_at, _verify_geometry) and recording_a_only.py (press_direction, apply_depth).
Only reviewed, statically parsed calibration data is supplied by the caller.
"""
import math
from types import SimpleNamespace
import numpy as np


class CalibratedGeometry:
    def __init__(self, data):
        self.CFG = {name: np.asarray(value, dtype=float) for name, value in
                    data["BOW_CONFIGS"][data["CONFIG_NAME"]].items()}
        self.FROG, self.TIP = self.CFG["frog"], self.CFG["tip"]
        self._tip_rotation = self._canonicalise_rotvec(self.TIP[3:], self.FROG[3:])
        self.BOW_LENGTH = float(np.linalg.norm(self.TIP[:3] - self.FROG[:3]))
        self.LIFT_M = float(min(np.linalg.norm(self.CFG["fsafe"][:3] - self.FROG[:3]),
                                np.linalg.norm(self.CFG["tsafe"][:3] - self.TIP[:3])))
        for key in ("U_MIN", "U_MAX", "MAX_OUTWARD_DEPTH", "MAX_INWARD_DEPTH",
                    "ACCEL_MAX", "CALIBRATED_SPEED_MAX"):
            setattr(self, key, data[key])
        self._rec = SimpleNamespace(**{key: data[key] for key in
                    ("VERIFIED_TCP", "VERIFIED_PAYLOAD_KG", "VERIFIED_PAYLOAD_COG")})

    @staticmethod
    def _rv_to_matrix(r):
        r = np.asarray(r, dtype=float)
        theta = float(np.linalg.norm(r))
        if theta < 1e-12: return np.eye(3)
        k = r / theta
        cross = np.array([[0.0, -k[2], k[1]], [k[2], 0.0, -k[0]], [-k[1], k[0], 0.0]])
        return np.eye(3) + math.sin(theta) * cross + (1.0 - math.cos(theta)) * (cross @ cross)

    @staticmethod
    def _canonicalise_rotvec(r, reference):
        r = np.asarray(r, dtype=float)
        theta = float(np.linalg.norm(r))
        if theta < 1e-12: return r
        alternative = r * (theta - 2.0 * math.pi) / theta
        return alternative if np.linalg.norm(alternative-reference) < np.linalg.norm(r-reference) else r

    def pose_at(self, u):
        u = float(np.clip(u, 0.0, 1.0))
        if u <= 1e-9: return self.FROG.copy()
        if u >= 1.0 - 1e-9: return self.TIP.copy()
        pose = np.empty(6, dtype=float)
        pose[:3] = self.FROG[:3] + u * (self.TIP[:3] - self.FROG[:3])
        pose[3:] = self.FROG[3:] + u * (self._tip_rotation - self.FROG[3:])
        return pose

    @staticmethod
    def press_direction(cfg):
        difference = cfg["fsafe"][:3] - cfg["frog"][:3]
        norm = np.linalg.norm(difference)
        return difference / norm if norm > 1e-9 else np.zeros(3)

    def apply_depth(self, pose, cfg, depth):
        depth = float(np.clip(depth, -self.MAX_OUTWARD_DEPTH, self.MAX_INWARD_DEPTH))
        out = np.array(pose, dtype=float).copy()
        out[:3] = out[:3] - depth * self.press_direction(cfg)
        return out

    def _verify_geometry(self):
        # Same endpoint/span/geodesic checks as upstream, kept active under -O.
        if not np.allclose(self.pose_at(0), self.FROG, atol=1e-12) or not np.allclose(self.pose_at(1), self.TIP, atol=1e-12):
            raise ValueError("geometry endpoints differ from calibration")
        frog, tip = self._rv_to_matrix(self.FROG[3:]), self._rv_to_matrix(self.TIP[3:])
        def angle(a, b):
            return math.degrees(math.acos(float(np.clip((np.trace(a.T @ b)-1)/2, -1, 1))))
        span = angle(frog, tip)
        if not math.isfinite(span) or span >= 10:
            raise ValueError("calibrated orientation span too large to interpolate")
        worst = max(abs(angle(frog, self._rv_to_matrix(self.pose_at(u)[3:])) - u*span)
                    for u in np.linspace(0.0, 1.0, 51))
        if not math.isfinite(worst) or worst >= .05:
            raise ValueError("interpolated rotation deviates from geodesic")
