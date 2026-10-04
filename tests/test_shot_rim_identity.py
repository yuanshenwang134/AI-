import unittest
from aihoop.hoop import Hoop, HoopConfig, HoopTrack, _select_rim_candidate

class RimIdentityTests(unittest.TestCase):
    def setUp(self):
        self.cfg=HoopConfig()
        self.prev=Hoop(100,100,20,8,confidence=.6)
    def test_near_identity_wins_over_more_confident_other_rim(self):
        near=Hoop(104,100,20,8,confidence=.4)
        other=Hoop(200,100,20,8,confidence=.99)
        self.assertIs(_select_rim_candidate([other,near],self.prev,self.cfg)[0],near)
    def test_distant_target_is_not_reacquired(self):
        got,why=_select_rim_candidate([Hoop(500,100,20,8)],self.prev,self.cfg)
        self.assertIsNone(got);self.assertEqual(why,'identity_gate_rejected')
    def test_size_jump_is_rejected(self):
        self.assertIsNone(_select_rim_candidate([Hoop(100,100,80,8)],self.prev,self.cfg)[0])
    def test_missing_detection_does_not_supply_fake_point(self):
        self.assertIsNone(_select_rim_candidate([],self.prev,self.cfg)[0])
    def test_small_camera_motion_allowed(self):
        h=Hoop(125,112,21,9)
        self.assertIs(_select_rim_candidate([h],self.prev,self.cfg)[0],h)
    def test_invalid_geometry_not_selected(self):
        self.assertIsNone(_select_rim_candidate([Hoop(float('nan'),100,20,8)],None,self.cfg)[0])
    def test_initial_lock_uses_confidence(self):
        h=Hoop(400,100,20,8,confidence=.9)
        self.assertIs(_select_rim_candidate([self.prev,h],None,self.cfg)[0],h)
    def test_trace_survives_serialization(self):
        trace=[{'t':0,'reason':'identity_gate_rejected','selected':None}]
        track=HoopTrack(samples=[(0,self.prev)],identity_trace=trace)
        self.assertEqual(track.to_dict()['identity_trace'],trace)

if __name__=='__main__': unittest.main()
