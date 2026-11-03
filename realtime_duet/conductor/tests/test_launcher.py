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
            self.assertEqual(spawn.call_args_list[0].kwargs["cwd"],
                             Path(integration_replay.__file__).resolve().parents[1]/"cello")
            return commands

    def test_default_video_launch_unifies_identity_and_is_only_mock(self):
        commands=self.build(["--video","synthetic-placeholder-not-opened.mp4"])
        r,s=commands["receiver"],commands["sender"]
        self.assertEqual(r[r.index("--session")+1],s[s.index("--session-id")+1])
        self.assertEqual(r[r.index("--clock-domain")+1],s[s.index("--clock-domain")+1])
        self.assertEqual(r[r.index("--listen")+1],s[s.index("--tcp-port")+1])
        self.assertEqual(r[r.index("--backend")+1],"mock")
        self.assertNotIn("--meter",r)  # Original receiver has no meter CLI option.
        self.assertNotIn("--enable-camera",s)
        self.assertFalse(commands["camera_enabled"])

    def test_future_camera_argument_is_explicit_without_opening_camera(self):
        commands=self.build(["--camera","0","--chin-y",".5"])
        s=commands["sender"]
        self.assertIn("--enable-camera",s)
        self.assertIn("--stdin-controls",s)
        self.assertNotIn("--video",s)
        self.assertFalse(commands["hardware_enabled"])

    def test_beat_only_spawns_exactly_one_sender_and_no_tcp_or_cello(self):
        with tempfile.TemporaryDirectory() as temp:
            output=Path(temp)/'run'
            args=['launch_rehearsal.py','--camera','0','--initial-bpm','90','--beat-only',
                  '--meter','3','--startup','count-in','--output',str(output)]
            with patch('sys.argv',args),patch('subprocess.Popen',return_value=FakeChild()) as spawn, \
                 patch('sys.stdout',io.StringIO()):
                self.assertEqual(integration_replay.main(),0)
            self.assertEqual(spawn.call_count,1)
            commands=json.loads((output/'commands.json').read_text())
            self.assertIsNone(commands['receiver']);self.assertFalse(commands['cello_enabled'])
            sender=commands['sender']
            self.assertEqual(sender[sender.index('--transport')+1],'file')
            self.assertEqual(sender[sender.index('--meter')+1],'3')
            self.assertNotIn('--wait-client',sender)
            self.assertNotIn('--chin-y',sender)
            self.assertNotIn('--calibration',sender)

    def test_three_meter_mock_rejects_before_creating_output_or_spawning(self):
        with tempfile.TemporaryDirectory() as temp:
            output=Path(temp)/'run'
            args=['launch_rehearsal.py','--video','not-opened.mp4','--initial-bpm','90',
                  '--meter','3','--output',str(output)]
            with patch('sys.argv',args),patch('subprocess.Popen') as spawn, \
                 patch('sys.stderr',io.StringIO()) as error:
                with self.assertRaises(SystemExit) as raised:integration_replay.main()
            self.assertEqual(raised.exception.code,2)
            self.assertIn('3/4 requires --beat-only',error.getvalue())
            self.assertFalse(output.exists())
            spawn.assert_not_called()

    def test_operator_stop_has_independent_priority_from_pending_start(self):
        run=VideoRun(types.SimpleNamespace())
        with patch("sys.stdin",io.StringIO("prepare\nstart\nstop\n")):
            run.read_operator_controls()
        self.assertTrue(run.stop_request.is_set())
        self.assertEqual(run.stop_info["reason"],"operator_pipe_stop")
        self.assertEqual(run.commands.qsize(),2)


if __name__=="__main__":
    unittest.main()
