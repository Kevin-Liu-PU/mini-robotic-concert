"""Reversible model capacity configuration; no camera or model download needed."""
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace as N
import unittest
from unittest.mock import Mock

from conductor.app import create_hand_landmarker, parse_args
from conductor.settings import load_config, ROOT
from tests.support.replay import matched


class HandModeTests(unittest.TestCase):
    def test_each_saved_profile_is_valid_and_two_hand_default_is_preserved(self):
        self.assertEqual(load_config()['num_hands'],2)
        self.assertEqual(load_config(ROOT/'config-two-hands.json')['num_hands'],2)
        self.assertEqual(load_config(ROOT/'config-single-hand.json')['num_hands'],1)

    def test_old_configs_keep_two_hand_behavior(self):
        cfg=json.loads((ROOT/'config.json').read_text());cfg.pop('num_hands')
        with tempfile.TemporaryDirectory() as temp:
            p=Path(temp)/'config.json';p.write_text(json.dumps(cfg))
            self.assertEqual(load_config(p)['num_hands'],2)

    def test_invalid_model_capacities_are_rejected(self):
        for capacity in (0,3,-1,True,1.0,'1',None):
            cfg=json.loads((ROOT/'config.json').read_text());cfg['num_hands']=capacity
            with tempfile.TemporaryDirectory() as temp:
                p=Path(temp)/'config.json';p.write_text(json.dumps(cfg))
                with self.assertRaises(ValueError):load_config(p)

    def test_cli_can_switch_either_direction(self):
        self.assertIsNone(parse_args([]).num_hands)
        for n in (1,2):self.assertEqual(parse_args(['--num-hands',str(n)]).num_hands,n)
        with self.assertRaises(SystemExit):parse_args(['--num-hands','3'])

    def test_actual_model_factory_receives_capacity_and_unchanged_thresholds(self):
        for n in (1,2):
            cfg=load_config();cfg['num_hands']=n
            factory=Mock(return_value=object())
            python=N(BaseOptions=N)
            vision=N(HandLandmarkerOptions=N,RunningMode=N(VIDEO='VIDEO'),
                     HandLandmarker=N(create_from_options=factory))
            result=create_hand_landmarker(cfg,python,vision)
            self.assertIs(result,factory.return_value)
            options=factory.call_args.args[0]
            self.assertEqual(options.num_hands,n)
            self.assertEqual(options.running_mode,'VIDEO')
            self.assertEqual(options.base_options.model_asset_path,cfg['model_path'])
            self.assertEqual(options.min_hand_detection_confidence,.6)
            self.assertEqual(options.min_hand_presence_confidence,.6)
            self.assertEqual(options.min_tracking_confidence,.6)

    def test_cross_model_beat_matching_cannot_reuse_one_event(self):
        result=matched([1,1.1,2],[1.08,2.02])
        self.assertEqual(result['retained'],2)
        self.assertEqual(result['missing'],[1.1])
        self.assertEqual(result['additional'],[])


if __name__=='__main__':unittest.main()
