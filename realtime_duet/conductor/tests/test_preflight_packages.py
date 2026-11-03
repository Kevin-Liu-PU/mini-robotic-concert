"""Distribution checks only: no third-party import or device access."""
import contextlib
import importlib.metadata
import io
import json
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import preflight


class PreflightPackagesTests(unittest.TestCase):
    def check(self, distributions):
        versions = dict(distributions, mediapipe='0.10.21', numpy='1.26.4')
        def version(name):
            if name not in versions:
                raise importlib.metadata.PackageNotFoundError(name)
            return versions[name]
        def module(name):
            if name == 'mediapipe':
                return SimpleNamespace(solutions=SimpleNamespace(holistic=object()))
            return SimpleNamespace()
        with patch.object(preflight.importlib, 'import_module', side_effect=module), \
             patch.object(preflight.importlib.metadata, 'version', side_effect=version), \
             contextlib.redirect_stdout(io.StringIO()) as out:
            result = preflight.main()
        return result, json.loads(out.getvalue())

    def test_only_contrib_is_valid(self):
        self.assertEqual(self.check({'opencv-contrib-python': '4.11.0.86'})[0], 0)

    def test_existing_gui_opencv_is_valid(self):
        self.assertEqual(self.check({'opencv-python': '4.11.0.86'})[0], 0)

    def test_two_cv2_distributions_are_rejected(self):
        self.assertEqual(self.check({'opencv-python': '4.11.0.86', 'opencv-contrib-python': '4.11.0.86'})[0], 1)

    def test_headless_is_rejected_for_calibration_window(self):
        self.assertEqual(self.check({'opencv-contrib-python-headless': '4.11.0.86'})[0], 1)


if __name__ == '__main__':
    unittest.main()
