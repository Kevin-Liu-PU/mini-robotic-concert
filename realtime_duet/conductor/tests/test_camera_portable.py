import types
import unittest
from conductor_rt.runner import camera_backend,frame_log_record
from conductor_rt.clock import MusicalClock,BeatObservation

class CameraPortableTests(unittest.TestCase):
    def test_platform_backend_selection_opens_no_device(self):
        cv=types.SimpleNamespace(CAP_AVFOUNDATION=1200,CAP_DSHOW=700,CAP_ANY=0)
        self.assertEqual(camera_backend(cv,'darwin'),(1200,'AVFoundation'))
        self.assertEqual(camera_backend(cv,'win32'),(700,'DirectShow'))
        self.assertEqual(camera_backend(cv,'linux'),(0,'OpenCV automatic'))

    def test_camera_log_excludes_image_and_keypoint_coordinates(self):
        metric={'model_ms':25,'frame':12}
        detection={'prepare':False,'stop':False,'visible':True,'association':'accepted',
                   'body':[.1,.2],'tip':[.2,.3],'features':{'pinch':.3},'observation':{'stamp':2}}
        result=frame_log_record(metric,detection,{'pose':[[.1,.2]]},camera=True)
        self.assertNotIn('landmarks',result)
        self.assertNotIn('body',result['detection'])
        self.assertNotIn('features',result['detection'])
        self.assertFalse(result['camera_landmarks_saved'])
        self.assertIn('landmarks',frame_log_record(metric,detection,{},camera=False))

    def test_count_in_tempo_source_is_reported_without_changing_phase(self):
        c=MusicalClock(80);c.arm(10)
        out=c.start(11.1,'causal_count_in_downbeat',BeatObservation(11,11.08,frame=1))
        self.assertEqual(c.tempo_evidence,'completed_causal_count_in')
        self.assertEqual(c.phase,0)
        self.assertEqual(out[0]['quality']['tempo_evidence'],'completed_causal_count_in')
        self.assertEqual(c.bpm,80)

if __name__=='__main__':unittest.main()
