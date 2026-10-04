import unittest
from unittest.mock import patch
from aihoop.sources import VideoSource, RawTrack
from aihoop.court import Calibration
from aihoop.hoop import Hoop
from aihoop.hoopsight import HoopSightError

class CenterHintTests(unittest.TestCase):
    def call(self, hoop):
        source=VideoSource('fixture.mp4',Calibration(),detect_players=False,manual_hoop=hoop)
        rt=RawTrack()
        with patch('aihoop.hoopsight.scan_hoopsight',side_effect=HoopSightError('test stop')) as scan:
            source._hoopsight_attempts(rt)
        return scan.call_args.kwargs,rt
    def test_center_only_is_hint_not_invalid_complete_hoop(self):
        args,rt=self.call(Hoop(621,181,0,0,method='manual'))
        self.assertIsNone(args['hoop_track'])
        self.assertEqual(args['hoop_hint'],(621,181))
        self.assertEqual(rt.detections_meta['hoop_source'],'manual_center+detector_geometry')
    def test_complete_ellipse_stays_manual(self):
        args,rt=self.call(Hoop(621,181,18,4,method='manual'))
        self.assertEqual(args['hoop_track'].samples[0][1].rx,18)
        self.assertEqual(rt.detections_meta['hoop_source'],'manual_landmarks')

if __name__=='__main__': unittest.main()
