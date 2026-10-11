"""Model-loader contracts without importing an SDK or opening any device."""
import contextlib
from pathlib import Path
import sys
import tempfile
from threading import Event
import types
import unittest
from unittest.mock import patch

from conductor import app
from conductor.startup import ResourceStartup, source_status_labels


@contextlib.contextmanager
def strict_sdk_modules():
    """Mirror the installed module namespace; tasks itself has no vision."""
    modules={name:types.ModuleType(name) for name in (
        'mediapipe','mediapipe.tasks','mediapipe.tasks.python','mediapipe.tasks.python.vision')}
    for value in modules.values():value.__path__=[]
    modules['mediapipe'].tasks=modules['mediapipe.tasks']
    modules['mediapipe.tasks'].python=modules['mediapipe.tasks.python']
    modules['mediapipe.tasks.python'].vision=modules['mediapipe.tasks.python.vision']
    missing=object();saved={name:sys.modules.get(name,missing) for name in modules}
    sys.modules.update(modules)
    try:yield modules
    finally:
        for name,value in saved.items():
            if value is missing:sys.modules.pop(name,None)
            else:sys.modules[name]=value


class SharedModelLoaderTests(unittest.TestCase):
    def test_installed_namespace_contract_and_background_transfer(self):
        with tempfile.TemporaryDirectory() as temp, strict_sdk_modules() as sdk:
            model_path=Path(temp)/'synthetic.task';model_path.write_text('stub')
            cfg={'model_path':str(model_path)};resource=object()
            self.assertFalse(hasattr(sdk['mediapipe.tasks'],'vision'))
            with self.assertRaises(ImportError):
                exec('from mediapipe.tasks import python, vision',{})
            with patch.object(app,'create_hand_landmarker',return_value=resource) as create:
                loader=ResourceStartup(lambda:app.load_hand_model(cfg),lambda value:None)
                loader.thread.join(1)
                try:
                    self.assertFalse(loader.thread.is_alive())
                    self.assertEqual(loader.take(),((sdk['mediapipe'],resource),None))
                    self.assertIsNone(loader.failure_traceback)
                    create.assert_called_once_with(cfg,sdk['mediapipe.tasks.python'],sdk['mediapipe.tasks.python.vision'])
                finally:loader.close()

    def test_missing_model_fails_before_sdk_factory(self):
        with tempfile.TemporaryDirectory() as temp, patch.object(app,'create_hand_landmarker') as create:
            with self.assertRaises(FileNotFoundError):
                app.load_hand_model({'model_path':str(Path(temp)/'missing.task')})
            create.assert_not_called()

    def test_background_failure_retains_full_exception_chain(self):
        def fail():
            try:raise ValueError('inner initialization cause')
            except ValueError as cause:raise RuntimeError('outer initialization failure') from cause
        loader=ResourceStartup(fail,lambda value:None);loader.thread.join(1)
        try:
            self.assertEqual(loader.take(),(None,'RuntimeError: outer initialization failure'))
            self.assertIn('Traceback (most recent call last):',loader.failure_traceback)
            self.assertIn('ValueError: inner initialization cause',loader.failure_traceback)
            self.assertIn('RuntimeError: outer initialization failure',loader.failure_traceback)
            self.assertIn('direct cause',loader.failure_traceback)
        finally:loader.close()


class SourceLabelTests(unittest.TestCase):
    def test_real_startup_has_no_synthetic_claim(self):
        mode,status=source_status_labels(False,{},True)
        self.assertEqual(mode,'Camera not initialized')
        self.assertIn('Starting camera/model',status)
        self.assertNotIn('Synthetic',mode+status)

    def test_real_startup_failure_is_explicit(self):
        mode,status=source_status_labels(False,{},True,'MODEL_INITIALIZATION_FAILED')
        self.assertEqual(mode,'Camera not initialized')
        self.assertEqual(status,'Startup failed: MODEL_INITIALIZATION_FAILED')
        self.assertNotIn('Synthetic',mode+status)

    def test_real_source_preserves_reported_mode_and_status(self):
        self.assertEqual(source_status_labels(False,{'mode':'1920x1080','status':'fresh'}),('1920x1080','fresh'))

    def test_only_demo_gets_synthetic_defaults(self):
        self.assertEqual(source_status_labels(True,{}),('Synthetic input / no camera','60 BPM > 120 BPM > pause'))
        self.assertEqual(source_status_labels(False,{}),('Camera not initialized','Waiting for camera/model'))


if __name__=='__main__':unittest.main()
