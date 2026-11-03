import unittest
from conductor_rt.dashboard import Dashboard,recording_feedback


class FakeButton:
    def __init__(self):self.values={}
    def configure(self,**values):self.values.update(values)


class RecordingFeedbackTests(unittest.TestCase):
    def test_click_immediately_disables_and_sends_only_once_until_ack(self):
        dashboard=Dashboard.__new__(Dashboard)
        dashboard.record_button=FakeButton();dashboard.record_stop_requested=False
        sent=[];dashboard.send=sent.append
        dashboard.request_record_stop();dashboard.request_record_stop()
        self.assertEqual(sent,['record-stop'])
        self.assertEqual(dashboard.record_button.values,dict(text='正在保存…',state='disabled'))
        # A stale active backend snapshot must not re-enable another click.
        label,state,status=recording_feedback(dict(active=True),dashboard.record_stop_requested)
        self.assertEqual(state,'disabled');self.assertIn('正在',label)
        label,state,status=recording_feedback(dict(finalized=True,written=1432),True)
        self.assertEqual(label,'已停录 ✓');self.assertEqual(state,'disabled');self.assertIn('1432',status)

    def test_error_does_not_claim_saved_and_disabled_recording_cannot_be_clicked(self):
        label,state,status=recording_feedback(dict(finalized=True,error='disk failure',written=12),True)
        self.assertEqual(label,'保存失败');self.assertIn('disk failure',status);self.assertEqual(state,'disabled')
        self.assertEqual(recording_feedback({})[1],'disabled')
        self.assertEqual(recording_feedback(dict(active=True))[1],'normal')


if __name__=='__main__':unittest.main()
