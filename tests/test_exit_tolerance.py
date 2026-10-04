import unittest
from aihoop.hoop import Hoop
from aihoop.hoopsight import SightConfig, _exit_observed, _shots_from_blob_series


class ExitToleranceTests(unittest.TestCase):
    def setUp(self):
        self.hoop=Hoop(cx=100,cy=100,rx=18,ry=3.9)
        self.cfg=SightConfig()

    def test_subpixel_shortfall_central_only(self):
        self.assertTrue(_exit_observed((0,101,103.75,100),self.hoop,self.cfg))
        self.assertFalse(_exit_observed((0,114,103.75,100),self.hoop,self.cfg))

    def test_above_center_and_large_shortfall_stay_rejected(self):
        self.assertFalse(_exit_observed((0,100,100,100),self.hoop,self.cfg))
        self.assertFalse(_exit_observed((0,100,103.4,100),self.hoop,self.cfg))

    def test_large_rim_tolerance_is_capped_at_half_pixel(self):
        hoop=Hoop(cx=100,cy=100,rx=62,ry=20)
        self.assertFalse(_exit_observed((0,100,119.4,100),hoop,self.cfg))

    def test_disabling_tolerance_restores_old_exit(self):
        self.cfg.exit_pixel_tolerance=0
        self.assertFalse(_exit_observed((0,101,103.75,100),self.hoop,self.cfg))
        self.assertTrue(_exit_observed((0,101,103.9,100),self.hoop,self.cfg))

    def test_chain_still_requires_cup_frames_and_marks_tolerance(self):
        chain=[(0,76,74,500),(.033,85,85,500),(.067,98,99,500),(.1,101,103.75,500)]
        shots=_shots_from_blob_series(chain,self.hoop,self.cfg)
        self.assertEqual(len(shots),1)
        self.assertIn('亚像素',shots[0].note)
        chain[2]=(.067,85,99,500)
        self.assertEqual(_shots_from_blob_series(chain,self.hoop,self.cfg),[])


if __name__=='__main__':
    unittest.main()
