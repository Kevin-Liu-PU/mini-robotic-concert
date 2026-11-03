import copy
import io
import json
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch
import numpy as np
import calibrate_chin
import integration_replay
from conductor_rt.camera_setup import make_calibration,load_calibration,validate_calibration,prepare_frame,camera_backend
from conductor_rt.runner import VideoRun

class FakeCapture:
    def __init__(self,opened=True,fail_read=False,shape=(720,1280,3)):
        self.opened=opened;self.fail_read=fail_read;self.shape=shape
        self.released=False;self.settings=[]
    def isOpened(self):return self.opened
    def set(self,k,v):self.settings.append((k,v));return True
    def get(self,k):return 30 if k==5 else self.shape[1] if k==3 else self.shape[0]
    def read(self):return (False,None) if self.fail_read else (True,np.zeros(self.shape,dtype=np.uint8))
    def release(self):self.released=True

class FakeCV:
    CAP_AVFOUNDATION=1200;CAP_DSHOW=700;CAP_ANY=0
    CAP_PROP_FRAME_WIDTH=3;CAP_PROP_FRAME_HEIGHT=4;CAP_PROP_FPS=5;CAP_PROP_BUFFERSIZE=38
    WINDOW_AUTOSIZE=1;EVENT_LBUTTONDOWN=1;FONT_HERSHEY_SIMPLEX=0;WND_PROP_VISIBLE=4
    def __init__(self,events=(),cap=None,gui_error=False,closed=False):
        self.events=list(events);self.cap=cap or FakeCapture();self.opens=[];self.callback=None
        self.destroyed=False;self.gui_error=gui_error;self.closed=closed;self.drawn=[];self.transforms=[]
    def VideoCapture(self,*args):self.opens.append(args);return self.cap
    def resize(self,image,size):self.transforms.append(('resize',size));return np.zeros((size[1],size[0],3),dtype=np.uint8)
    def flip(self,image,axis):self.transforms.append(('flip',axis));return image
    def namedWindow(self,name,flag):self.window_flag=flag
    def setMouseCallback(self,name,callback):self.callback=callback
    def line(self,image,start,end,color,width):self.drawn.append(start[1])
    def putText(self,*args):pass
    def imshow(self,*args):
        if self.gui_error:raise RuntimeError('fake GUI failure')
    def waitKey(self,delay):
        key,click=self.events.pop(0) if self.events else (27,None)
        if click is not None:self.callback(self.EVENT_LBUTTONDOWN,*click,0,None)
        return key
    def getWindowProperty(self,*args):return 0 if self.closed else 1
    def destroyWindow(self,name):self.destroyed=True

class CalibrationTests(unittest.TestCase):
    def test_click_confirm_saves_only_numeric_configuration_and_releases(self):
        fake=FakeCV([(-1,(320,180)),(13,None)])
        with tempfile.TemporaryDirectory() as temp:
            out=Path(temp)/'chin.json';data=calibrate_chin.calibrate(fake,0,out)
            self.assertEqual(data['chin_y'],.5)
            self.assertEqual(data['capture_width'],1280)
            self.assertEqual(data['preview_height'],360)
            self.assertTrue(all(type(v) in (int,float) for v in data.values()))
            self.assertEqual(list(Path(temp).iterdir()),[out])
            self.assertEqual(json.loads(out.read_text()),data)
        self.assertTrue(fake.cap.released and fake.destroyed)
        self.assertEqual(fake.window_flag,fake.WINDOW_AUTOSIZE)
        self.assertIn(180,fake.drawn)
        self.assertIn(('resize',(640,360)),fake.transforms)
        self.assertIn(('flip',1),fake.transforms)

    def test_cancel_after_click_writes_nothing_and_releases(self):
        fake=FakeCV([(-1,(320,180)),(27,None)])
        with tempfile.TemporaryDirectory() as temp:
            self.assertIsNone(calibrate_chin.calibrate(fake,0,Path(temp)/'chin.json'))
            self.assertEqual(list(Path(temp).iterdir()),[])
        self.assertTrue(fake.cap.released and fake.destroyed)

    def test_enter_without_selection_and_out_of_bounds_never_save(self):
        fake=FakeCV([(13,None),(13,(-1,100)),(13,(640,100)),(13,(100,0)),(13,(100,360)),(27,None)])
        with tempfile.TemporaryDirectory() as temp:
            self.assertIsNone(calibrate_chin.calibrate(fake,0,Path(temp)/'chin.json'))
            self.assertEqual(list(Path(temp).iterdir()),[])

    def test_click_and_enter_requires_line_to_be_displayed_before_save(self):
        fake=FakeCV([(13,(320,180)),(27,None)])
        with tempfile.TemporaryDirectory() as temp:
            self.assertIsNone(calibrate_chin.calibrate(fake,0,Path(temp)/'chin.json'))
            self.assertEqual(list(Path(temp).iterdir()),[])

    def test_window_close_cancels(self):
        fake=FakeCV([(-1,None)],closed=True)
        with tempfile.TemporaryDirectory() as temp:
            self.assertIsNone(calibrate_chin.calibrate(fake,0,Path(temp)/'chin.json'))
        self.assertTrue(fake.cap.released and fake.destroyed)

    def test_read_and_gui_errors_release_camera(self):
        for fake in (FakeCV(cap=FakeCapture(fail_read=True)),FakeCV(gui_error=True)):
            with tempfile.TemporaryDirectory() as temp:
                with self.assertRaises(RuntimeError):calibrate_chin.calibrate(fake,0,Path(temp)/'chin.json')
                self.assertEqual(list(Path(temp).iterdir()),[])
            self.assertTrue(fake.cap.released)
            if fake.gui_error:self.assertTrue(fake.destroyed)

    def test_open_failure_releases_and_never_creates_file(self):
        fake=FakeCV(cap=FakeCapture(opened=False))
        with tempfile.TemporaryDirectory() as temp:
            with self.assertRaises(RuntimeError):calibrate_chin.calibrate(fake,0,Path(temp)/'chin.json')
            self.assertEqual(list(Path(temp).iterdir()),[])
        self.assertTrue(fake.cap.released)

    def test_existing_output_is_never_overwritten_or_camera_opened(self):
        fake=FakeCV()
        with tempfile.TemporaryDirectory() as temp:
            p=Path(temp)/'chin.json';p.write_text('preserve')
            with self.assertRaises(FileExistsError):calibrate_chin.calibrate(fake,0,p)
            self.assertEqual(p.read_text(),'preserve');self.assertEqual(fake.opens,[])

    def test_invalid_config_and_mismatched_camera_width_backend_dimensions(self):
        good=make_calibration(.5,0,1200,1280,720,640)
        for key,value in [('chin_y',float('nan')),('chin_y',1),('chin_y',True),('camera_index',1),('processing_width',1280),('mirror_horizontal',0),('preview_height',480)]:
            d=copy.copy(good);d[key]=value
            with self.subTest(key=key,value=value),self.assertRaises(ValueError):validate_calibration(d,0,640)
        with self.assertRaises(ValueError):validate_calibration(good,0,640,700)
        with self.assertRaises(ValueError):validate_calibration(good,0,640,1200,(1920,1080))
        self.assertEqual(validate_calibration(good,0,640,1200,(1280,720)),good)

    def test_launcher_defaults_to_automatic_reference_without_calibration(self):
        import test_launcher
        command=test_launcher.LauncherTests().build(['--camera','0'])
        self.assertNotIn('--chin-y',command['sender'])
        self.assertNotIn('--calibration',command['sender'])
        self.assertIn('--enable-camera',command['sender'])

    def test_launcher_calibration_file_is_forwarded_not_downgraded_to_number(self):
        import test_launcher
        with tempfile.TemporaryDirectory() as temp:
            cfg=Path(temp)/'chin.json';cfg.write_text(json.dumps(make_calibration(.4,0,camera_backend(FakeCV())[0],1280,720,640)))
            command=test_launcher.LauncherTests().build(['--camera','0','--calibration',str(cfg)])
            self.assertIn('--calibration',command['sender']);self.assertNotIn('--chin-y',command['sender'])

    def test_camera_backend_mismatch_rejects_before_device_open(self):
        fake=FakeCV();backend,_=camera_backend(fake)
        cfg=make_calibration(.5,0,1200 if backend!=1200 else 700,1280,720,640)
        run=VideoRun(types.SimpleNamespace(enable_camera=True,camera_index=0,width=640,calibration_data=cfg))
        with patch.dict('sys.modules',{'cv2':fake}):run.read_camera()
        self.assertEqual(fake.opens,[]);self.assertTrue(run.reader_done.is_set());self.assertTrue(run.stop_request.is_set())

    def test_launcher_wrong_platform_backend_starts_zero_children(self):
        for platform,wrong_backend in [('win32',1200),('darwin',700),('linux',1200)]:
            with self.subTest(platform=platform),tempfile.TemporaryDirectory() as temp:
                cfg=Path(temp)/'chin.json';out=Path(temp)/'run'
                cfg.write_text(json.dumps(make_calibration(.5,0,wrong_backend,1280,720,640)))
                args=['launch_rehearsal.py','--camera','0','--initial-bpm','90','--calibration',str(cfg),'--output',str(out)]
                with patch('conductor_rt.camera_setup.sys.platform',platform),patch('sys.argv',args),patch('subprocess.Popen') as spawn,patch('sys.stderr',io.StringIO()) as err:
                    with self.assertRaises(SystemExit) as exc:integration_replay.main()
                    self.assertEqual(exc.exception.code,2)
                    self.assertIn('backend/platform differs',err.getvalue())
                    spawn.assert_not_called();self.assertFalse(out.exists())

    def test_preflight_backend_ids_match_installed_opencv_constants(self):
        import cv2
        for platform in ('darwin','win32','linux'):
            self.assertEqual(camera_backend(platform=platform),camera_backend(cv2,platform))

    def test_actual_frame_mismatch_releases_before_inference_queue(self):
        fake=FakeCV();backend,_=camera_backend(fake)
        cfg=make_calibration(.5,0,backend,1920,1080,640)
        run=VideoRun(types.SimpleNamespace(enable_camera=True,camera_index=0,width=640,calibration_data=cfg))
        with patch.dict('sys.modules',{'cv2':fake}):run.read_camera()
        self.assertTrue(fake.cap.released);self.assertEqual(run.decoded,0);self.assertIsNone(run.frames.take())

    def test_shared_transform_matches_previous_runner_exactly_without_camera(self):
        import cv2
        raw=np.arange(197*331*3,dtype=np.uint8).reshape(197,331,3)
        old=cv2.flip(cv2.resize(raw,(640,round(197*640/331))),1)
        self.assertTrue(np.array_equal(old,prepare_frame(cv2,raw,640)))

if __name__=='__main__':unittest.main()
