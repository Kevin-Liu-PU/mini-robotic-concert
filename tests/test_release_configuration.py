"""Portable configuration and output enumeration contracts, with fake devices."""
import contextlib
import io
import json
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from conductor.audio_clock import AudioConfig
from conductor.settings import load_config
from tools.list_audio_devices import main as list_outputs

ROOT = Path(__file__).resolve().parents[1]


class ReleaseConfigurationTests(unittest.TestCase):
    def test_public_camera_profiles_have_no_named_device_binding(self):
        for path in ROOT.glob('config*.json'):
            data = json.loads(path.read_text())
            if 'camera_index' in data:
                self.assertEqual(data['camera_index'], 0, path.name)
                self.assertEqual(data['camera_name'], '', path.name)
                cfg = load_config(path)
                self.assertEqual((cfg['detector']['min_bpm'],cfg['detector']['max_bpm']), (25,180))
                if 'screen_clock' in cfg:
                    self.assertEqual((cfg['screen_clock']['min_bpm'],cfg['screen_clock']['max_bpm']), (25,180))

    def test_generic_audio_uses_system_default_and_retains_optional_name_guard(self):
        data = json.loads((ROOT/'config-audio.json').read_text())
        cfg = AudioConfig(**data)
        self.assertIsNone(cfg.device)
        self.assertIsNone(cfg.device_name_contains)
        self.assertEqual(cfg.peak_amplitude, .08)

    def test_audio_listing_filters_input_only_devices_without_opening_streams(self):
        fake = SimpleNamespace(query_devices=lambda:[
            dict(name='Input only',hostapi=0,max_output_channels=0),
            dict(name='Example output',hostapi=0,max_output_channels=2)],
            query_hostapis=lambda:[dict(name='Example host')])
        output = io.StringIO()
        with patch.dict('sys.modules', {'sounddevice':fake}), contextlib.redirect_stdout(output):
            self.assertEqual(list_outputs(), 0)
        self.assertEqual(json.loads(output.getvalue()),[
            dict(index=1,name='Example output',host_api='Example host',output_channels=2)])
