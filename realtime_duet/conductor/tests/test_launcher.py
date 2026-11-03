import io
import json
from pathlib import Path
import tempfile
import types
import unittest
from unittest.mock import patch
import integration_replay
from conductor_rt.runner import VideoRun


class FakeChild:
    returncode=0
    stdin=None
    def poll(self):return 0
    def wait(self,timeout=None):return 0


class LauncherTests(unittest.TestCase):
    def build(self,source):
        with tempfile.TemporaryDirectory() as temp:
            output=Path(temp)/"run"
            args=["launch_rehearsal.py",*source,"--initial-bpm","60","--output",str(output)]
            with patch("sys.argv",args),patch("subprocess.Popen",return_value=FakeChild()) as spawn,patch("time.sleep"),patch("sys.stdout",io.StringIO()):
                self.assertEqual(integration_replay.main(),0)
            commands=json.loads((output/"commands.json").read_text())
            self.assertEqual(spawn.call_count,2)
            return commands

    def test_default_video_launch_unifies_identity_and_is_only_mock(self):
        commands=self.build(["--video","synthetic-placeholder-not-opened.mp4"])
        r,s=commands["receiver"],commands["sender"]
        self.assertEqual(r[r.index("--session")+1],s[s.index("--session-id")+1])
        self.assertEqual(r[r.index("--clock-domain")+1],s[s.index("--clock-domain")+1])
        self.assertEqual(r[r.index("--listen")+1],s[s.index("--tcp-port")+1])
        self.assertEqual(r[r.index("--backend")+1],"mock")
        self.assertNotIn("--enable-camera",s)
        self.assertFalse(commands["camera_enabled"])

    def test_future_camera_argument_is_explicit_without_opening_camera(self):
        commands=self.build(["--camera","0","--chin-y",".5"])
        s=commands["sender"]
        self.assertIn("--enable-camera",s)
        self.assertIn("--stdin-controls",s)
        self.assertNotIn("--video",s)
        self.assertFalse(commands["hardware_enabled"])

    def test_operator_stop_has_independent_priority_from_pending_start(self):
        run=VideoRun(types.SimpleNamespace())
        with patch("sys.stdin",io.StringIO("prepare\nstart\nstop\n")):
            run.read_operator_controls()
        self.assertTrue(run.stop_request.is_set())
        self.assertEqual(run.stop_info["reason"],"operator_pipe_stop")
        self.assertEqual(run.commands.qsize(),2)


if __name__=="__main__":
    unittest.main()
