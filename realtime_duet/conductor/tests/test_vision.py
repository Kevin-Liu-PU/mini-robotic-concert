import unittest
from conductor_rt.vision import hand_features, VisualDetector


class GeometryTests(unittest.TestCase):
    def test_physical_shape_features_invariant_to_image_aspect(self):
        physical=[(.1+(i%5)*.03,.2+(i//5)*.025) for i in range(21)]
        wide=[(x/(16/9),y) for x,y in physical]
        portrait=[(x/(9/16),y) for x,y in physical]
        a=hand_features(wide,16/9)
        b=hand_features(portrait,9/16)
        for key in a:
            self.assertAlmostEqual(a[key],b[key],places=7)

    def test_missing_body_cannot_invent_prepare_stop_or_beat(self):
        d=VisualDetector(.55)
        for i in range(20):
            out=d.update(dict(pose=None,hands=[]),i*.033,i*.033+.03,i*.033,i,90)
            self.assertFalse(out["prepare"])
            self.assertFalse(out["stop"])
            self.assertIsNone(out["observation"])

    def test_stop_requires_real_elapsed_confirmation_and_stationarity(self):
        d=VisualDetector(.55)
        d.scale=.3
        out=[]
        for i in range(9):
            out.append(d.confirm_stop(i/30,(.5,.3),True,True))
        self.assertFalse(any(x[0] for x in out[:7]))
        self.assertTrue(out[-1][0])
        self.assertGreaterEqual(8/30-out[-1][1],.22)
        moving=VisualDetector(.55)
        moving.scale=.3
        for i in range(15):
            self.assertFalse(moving.confirm_stop(i/30,(.5,.3+i*.015),True,True)[0])


if __name__=="__main__":
    unittest.main()
