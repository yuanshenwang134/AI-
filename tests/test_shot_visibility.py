"""Synthetic trajectories only: no video decoding or detector inference."""
import unittest
from aihoop.ball import BallCandidate
from aihoop.hoop import Hoop, HoopTrack, detect_shots


def arc(end_x=500):
    pts = []
    for i in range(31):
        t = i / 30
        k = min(1, t * 2)
        x = 100 + 400 * k if i <= 15 else 500 + (end_x - 500) * (t-.5)*2
        y = 400 - 560*t if i <= 15 else 120 + 560*(t-.5)
        pts.append(BallCandidate(t=t, x=x, y=y, score=.9, source='test'))
    return pts


class ShotVisibilityTests(unittest.TestCase):
    def setUp(self):
        self.hoop = Hoop(cx=500, cy=200, rx=30, ry=10)

    def test_visible_make_and_miss(self):
        self.assertIs(detect_shots([arc()], self.hoop)[0].made, True)
        self.assertIs(detect_shots([arc(680)], self.hoop)[0].made, False)

    def test_out_of_frame_is_unknown_and_release_is_not_shifted(self):
        shot = detect_shots([arc()[:16]], self.hoop)[0]
        self.assertIsNone(shot.made)
        self.assertEqual(shot.t, 0)
        self.assertEqual(shot.release_x, 100)

    def test_rim_edge_is_ambiguous(self):
        # Crossing at x=530: overlaps the edge, not a clear miss.
        shot = detect_shots([arc(605)], self.hoop)[0]
        self.assertIsNone(shot.made)

    def test_long_occlusion_does_not_invent_crossing(self):
        points = arc()
        for p in points[20:]:
            p.t += 2
        shots = detect_shots([points], self.hoop)
        self.assertFalse(any(s.made is True for s in shots))

    def test_short_occlusion_at_rim_does_not_interpolate_a_make(self):
        points = arc()
        for p in points[20:]:
            p.t += .3
        shot = detect_shots([points], self.hoop)[0]
        self.assertIsNone(shot.made)

    def test_unknown_survives_visual_attempt_conversion(self):
        from unittest.mock import patch
        from aihoop.sources import RawTrack, VideoSource
        from aihoop.court import Calibration
        source = VideoSource('stub.mp4', Calibration(), detect_players=False)
        source._tracks_px = [arc()[:16]]
        ht = HoopTrack(samples=[(0,self.hoop)])
        rt = RawTrack()
        rt.detections_meta['calibration_valid'] = False
        with patch('aihoop.hoop.detect_hoop_track', return_value=ht), \
             patch('aihoop.ball.rank_ball_tracks', return_value=source._tracks_px), \
             patch.object(source, '_jump_shooter', return_value=None), \
             patch.object(source, '_locate_shooter', return_value=(None,None)), \
             patch.object(source, '_estimate_shot_value', return_value=(2,'visual_estimate')):
            source._visual_attempts(rt)
        self.assertEqual(len(rt.attempts), 1)
        self.assertIsNone(rt.attempts[0].made)
        self.assertIn('needs_review', rt.attempts[0].tags)
        self.assertEqual(rt.detections_meta['visual']['unknown'],1)

    def test_cut_does_not_bridge_two_scenes(self):
        ht = HoopTrack(samples=[(0,self.hoop),(.65,self.hoop)], fps=30,
                       duration=1, votes=2, frames=2, cut_times=[.65], smooth_window=0)
        self.assertFalse(any(s.made is True for s in detect_shots([arc()], ht)))

    def test_camera_translation_preserves_crossing(self):
        original = arc()
        samples=[]
        for p in original:
            dx,dy = 150*p.t, 200*p.t
            samples.append((p.t,Hoop(cx=500+dx,cy=200+dy,rx=30,ry=10)))
            p.x += dx
            p.y += dy
        ht=HoopTrack(samples=samples,fps=30,duration=1,votes=31,frames=31,smooth_window=0)
        shot=detect_shots([original],ht)[0]
        self.assertIs(shot.made,True)
        self.assertAlmostEqual(shot.crossing_t,.5+80/560)

    def test_camera_pan_does_not_turn_stationary_object_into_shot(self):
        samples=[]; points=[]
        for p in arc():
            samples.append((p.t,Hoop(cx=500+p.x,cy=200+p.y,rx=30,ry=10)))
            points.append(BallCandidate(t=p.t,x=500+p.x,y=200+p.y-40,score=.9,source='test'))
        ht=HoopTrack(samples=samples,fps=30,duration=1,votes=31,frames=31,smooth_window=0)
        self.assertEqual(detect_shots([points],ht),[])

    def test_duplicate_track_keeps_one_event(self):
        self.assertEqual(len(detect_shots([arc(),arc()],self.hoop)),1)

    def test_invalid_point_breaks_evidence(self):
        points=arc(); points[18].x=float('nan')
        self.assertFalse(any(s.made is True for s in detect_shots([points],self.hoop)))


if __name__ == '__main__':
    unittest.main()
