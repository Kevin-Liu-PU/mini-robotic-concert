"""Optional OpenCV integration checks; all images are generated synthetic data."""
import unittest
from copy import deepcopy
from unittest.mock import patch

from conductor.core import TempoMetronome
from conductor.multitrack import ConductorSession
from conductor.synthetic import demo_hand
try:
    import cv2
    import numpy as np
    from conductor.display import render, ORANGE
except ImportError:
    cv2=None


@unittest.skipIf(cv2 is None,'OpenCV is needed only for dashboard integration tests')
class TipDisplayTests(unittest.TestCase):
    def scene(self):
        session=ConductorSession()
        for i in range(30):
            session.update([demo_hand(.25,.6)],i/30)
        info=dict(demo=True,capture_fps=0,processing_fps=30,inference_ms=0,
                  primary='A',primary_status=session.display_status(),
                  hand_label=session.display_status(),camera_mode='Synthetic',
                  camera_name='Synthetic',camera_status='No camera')
        return session,info

    def draw(self,session,info,frame):
        return render(frame,session.engine,TempoMetronome(),1,session.trajectories,[],
                      {'trajectory_seconds':3},info,tracks=session.tracker.tracks)

    def test_filled_main_dot_maps_observed_tip_not_wrist_in_letterboxed_view(self):
        for width,height in ((960,540),(640,480)):
            session,info=self.scene()
            frame=np.zeros((height,width,3),np.uint8)
            with patch.object(cv2,'circle',wraps=cv2.circle) as circles:
                canvas=self.draw(session,info,frame)
            scale=min(864/width,486/height)
            iw,ih=round(width*scale),round(height*scale)
            expected=((864-iw)//2+int(.25*iw),(486-ih)//2+int(.6*ih))
            filled_main=[call.args[1] for call in circles.call_args_list
                         if call.args[2:5]==(8,ORANGE,-1)]
            self.assertEqual(filled_main,[expected])
            self.assertEqual(canvas.shape,(800,1360,3))

    def test_invalid_tip_hides_dot_and_invalid_skeleton_points_do_not_crash_ui(self):
        for invalid in ('absent','nan','none_coordinate'):
            session,info=self.scene()
            hand=demo_hand(.25,.6)
            if invalid=='absent':
                hand.landmarks[8]=None
            else:
                hand.landmarks[8].y=float('nan') if invalid=='nan' else None
            session.update([hand],1)
            with patch.object(cv2,'circle',wraps=cv2.circle) as circles:
                self.draw(session,info,np.zeros((540,960,3),np.uint8))
            self.assertIsNone(session.engine.raw)
            self.assertFalse(any(call.args[2:5]==(8,ORANGE,-1) for call in circles.call_args_list))

    def test_labels_identify_actual_tip_point_and_tempo_only_output(self):
        session,info=self.scene()
        with patch.object(cv2,'putText',wraps=cv2.putText) as labels:
            self.draw(session,info,np.zeros((540,960,3),np.uint8))
        texts=[call.args[1] for call in labels.call_args_list]
        self.assertIn('INDEX TIP HEIGHT / LAST 8 SECONDS',texts)
        self.assertIn('A MAIN TIP',texts)
        self.assertIn('Free-running / NO phase following',texts)
        self.assertFalse(any('WRIST' in text or 'PALM' in text for text in texts))

    def test_single_hand_dashboard_names_operating_restriction(self):
        session,info=self.scene()
        with patch.object(cv2,'putText',wraps=cv2.putText) as labels:
            render(np.zeros((540,960,3),np.uint8),session.engine,TempoMetronome(),1,
                   session.trajectories,[],{'trajectory_seconds':3,'num_hands':1},info,
                   tracks=session.tracker.tracks)
        texts=[call.args[1] for call in labels.call_args_list]
        self.assertIn('SINGLE-HAND MODE: SHOW ONLY ONE HAND',texts)
        self.assertFalse(any('B: PURPLE' in label for label in texts))
        self.assertIn('Free-running / NO phase following',texts)

    def test_beat_preparation_and_recording_ready_have_distinct_labels(self):
        session,info=self.scene()
        for i in range(30,60):
            session.update([demo_hand(.25,.4)],i/30)
        self.assertTrue(session.engine.armed)
        info['recording']=dict(state='READY',dropped_frames=0,diagnostic_drops=0)
        before=deepcopy(vars(session.engine))
        with patch.object(cv2,'putText',wraps=cv2.putText) as labels:
            self.draw(session,info,np.zeros((540,960,3),np.uint8))
        texts=[call.args[1] for call in labels.call_args_list]
        self.assertIn('Beat armed - move down to touch / cross line',texts)
        self.assertIn('Recording READY - press C',texts)
        self.assertEqual(vars(session.engine),before)

    def test_preparation_labels_preserve_acquisition_and_lost_status(self):
        session,info=self.scene()
        for status,expected in (
            ('LIFT FROM LINE TO READY','Return above line to prepare the next beat'),
            ('ACQUIRING - RETURN UP','Acquiring hand - return above line to prepare'),
            ('PAUSED - RETURN UP TO REARM','Beat paused - return above line to prepare'),
            ('LOST TRACK - SHOW ONE HAND','LOST TRACK - SHOW ONE HAND'),
        ):
            session.engine.status=status
            with patch.object(cv2,'putText',wraps=cv2.putText) as labels:
                self.draw(session,info,np.zeros((540,960,3),np.uint8))
            texts=[call.args[1] for call in labels.call_args_list]
            self.assertIn(expected,texts)
            self.assertFalse(any(label.startswith('Beat armed') for label in texts))

    def test_recording_ready_waiting_label_fits_banner(self):
        session,info=self.scene()
        info['recording']=dict(state='READY',dropped_frames=0,diagnostic_drops=0)
        info['recording_source_ready']=False
        with patch.object(cv2,'putText',wraps=cv2.putText) as labels:
            self.draw(session,info,np.zeros((540,960,3),np.uint8))
        call=next(call for call in labels.call_args_list
                  if call.args[1]=='Recording READY - waiting for camera')
        width=cv2.getTextSize(call.args[1],call.args[3],call.args[4],call.args[6])[0][0]
        self.assertLessEqual(call.args[2][0]+width,1334)


if __name__=='__main__':
    unittest.main()
