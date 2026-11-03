import sys
import unittest
from unittest.mock import patch
import launch_beat_test


class BeatSetupTests(unittest.TestCase):
    def test_selected_meter_reaches_only_beat_launcher(self):
        seen=[]
        def main():seen.append(list(sys.argv));return 0
        with patch('sys.argv',['launch_beat_test.py','--camera','0','--initial-bpm','90','--meter','4']), \
             patch('launch_beat_test.choose_test_settings',return_value=dict(meter=3,duration=180,record=True,hand='left',audio=True)),patch('launch_beat_test.main',side_effect=main):
            self.assertEqual(launch_beat_test.run(),0)
        args=seen[0]
        self.assertEqual(args[args.index('--meter')+1],'3')
        self.assertIn('--beat-only',args);self.assertIn('--preview',args)
        self.assertIn('--record-video',args);self.assertEqual(args[args.index('--duration')+1],'180')
        self.assertIn('--beat-audio',args)
        self.assertEqual(args[args.index('--conducting-hand')+1],'left')

    def test_restart_changes_session_output_and_reopens_meter_choice(self):
        seen=[]
        def main():seen.append(list(sys.argv));return 75 if len(seen)==1 else 0
        with patch('sys.argv',['launch_beat_test.py','--camera','0','--initial-bpm','90','--session','first',
                               '--clock-domain','local-first','--output','/tmp/beat-test-fixture']), \
             patch('launch_beat_test.choose_test_settings',side_effect=[dict(meter=3,duration=180,record=True,hand='left'),dict(meter=4,duration=300,record=False,hand='right')]),patch('launch_beat_test.main',side_effect=main):
            launch_beat_test.run()
        self.assertEqual([a[a.index('--meter')+1] for a in seen],['3','4'])
        self.assertEqual([a[a.index('--conducting-hand')+1] for a in seen],['left','right'])
        self.assertNotEqual(seen[1][seen[1].index('--session')+1],'first')
        self.assertNotEqual(seen[1][seen[1].index('--output')+1],'/tmp/beat-test-fixture')

    def test_cancel_setup_opens_no_camera_process(self):
        with patch('sys.argv',['launch_beat_test.py']),patch('launch_beat_test.choose_test_settings',return_value=None), \
             patch('launch_beat_test.main') as main:
            self.assertEqual(launch_beat_test.run(),0)
        main.assert_not_called()

if __name__=='__main__':unittest.main()
