import subprocess
import sys
import tempfile
from pathlib import Path
import unittest


class BoundaryTests(unittest.TestCase):
    def test_camera_without_enable_exits_before_any_device_or_output(self):
        with tempfile.TemporaryDirectory() as temp:
            output=Path(temp)/"must-not-exist"
            r=subprocess.run([sys.executable,"-B","-m","conductor_rt","--camera-index","0",
                              "--initial-bpm","60","--chin-y",".55","--clock-domain","guard-test",
                              "--output",str(output)],capture_output=True,text=True,timeout=5)
            self.assertEqual(r.returncode,2)
            self.assertIn("no camera was opened",r.stderr)
            self.assertFalse(output.exists())

    def test_no_implicit_bpm_on_cli(self):
        r=subprocess.run([sys.executable,"-B","-m","conductor_rt","--camera-index","0",
                          "--chin-y",".55","--clock-domain","guard-test","--output","unused"],
                         capture_output=True,text=True,timeout=5)
        self.assertEqual(r.returncode,2)
        self.assertIn("--initial-bpm",r.stderr)


if __name__=="__main__":
    unittest.main()
