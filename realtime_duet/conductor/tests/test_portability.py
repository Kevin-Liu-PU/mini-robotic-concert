import sys
import unittest
from unittest.mock import patch
from conductor_rt import timing
import test_launcher

class PortabilityTests(unittest.TestCase):
    def test_non_windows_wait_never_loads_windows_library(self):
        with patch.object(timing.os,'name','posix'),patch.object(timing,'_Timer') as timer,patch.object(timing.time,'sleep') as sleep:
            timing.precise_wait(.002)
            timer.assert_not_called();sleep.assert_called_once_with(.002)

    def test_default_interpreters_are_current_environment(self):
        commands=test_launcher.LauncherTests().build(['--video','placeholder.mp4'])
        self.assertEqual(commands['sender'][0],sys.executable)
        self.assertEqual(commands['receiver'][0],sys.executable)

if __name__=='__main__':unittest.main()
