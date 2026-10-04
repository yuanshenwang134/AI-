"""Synthetic checks: inferred trajectories never become observed outcomes."""
import unittest
from aihoop.hoop import HoopConfig, HoopTrack, detect_shots
from aihoop.sources import Attempt
from aihoop.rules import build_shots
import test_cross_metric


class ShotEvidenceTests(unittest.TestCase):
    def setUp(self):
        fixture = test_cross_metric.ExtrapolatedCrossingTests()
        fixture.setUp()
        self.track, self.hoop = fixture.track, fixture.hoop

    def shot(self, **kwargs):
        return detect_shots([self.track], self.hoop, HoopConfig(**kwargs))[0]

    def test_interpolation_is_not_measured(self):
        self.assertEqual(self.shot(allow_hole_interpolation=True).evidence,
                         'cross_interpolated')

    def test_fit_cannot_bypass_drop_floor(self):
        self.assertIsNot(self.shot(allow_extrapolated_crossing=True,
                                  min_cross_drop_m=1.0).made, True)

    def test_fit_cannot_bypass_max_gap(self):
        self.assertIsNot(self.shot(allow_extrapolated_crossing=True,
                                  max_cross_gap_s=.25).made, True)

    def test_inference_survives_conversion_and_remains_unknown(self):
        for flag in ['allow_hole_interpolation', 'allow_extrapolated_crossing']:
            with self.subTest(flag=flag):
                s = self.shot(**{flag: True})
                a = Attempt(t=s.t, team='home', player_id='H1', x=0, y=0,
                            made=s.made, conf=s.confidence, source='ball_rim')
                a.attach_shot_evidence(s)
                restored = Attempt(**a.to_dict())
                self.assertIsNone(restored.made)
                self.assertIs(restored.suggested_made, True)
                self.assertEqual(restored.crossing_t, s.crossing_t)
                result = build_shots([restored.to_dict()], [])[0]
                self.assertEqual(result.result, 'unknown')
                self.assertIn(s.evidence, result.tags)
                self.assertIn('needs_review', result.tags)

    def test_measured_prior_is_preserved(self):
        s = self.shot(allow_hole_interpolation=True)
        s.evidence = 'cross_measured'
        a = Attempt(t=s.t, team='home', player_id='H1', x=0, y=0,
                    made=True, conf=.9, source='ball_rim')
        a.attach_shot_evidence(s)
        self.assertIs(a.made, True)
        self.assertIsNone(a.suggested_made)
        self.assertEqual(build_shots([a.to_dict()], [])[0].result, 'made')

    def test_hoop_export_preserves_all_sample_times(self):
        samples = [(i/30, self.hoop) for i in range(12)]
        data = HoopTrack(samples=samples).to_dict()
        self.assertTrue(data['samples_complete'])
        self.assertEqual(len(data['samples']), len(samples))
        for (t, _), (saved, _) in zip(samples, data['samples']):
            self.assertAlmostEqual(t, saved, places=6)


if __name__ == '__main__':
    unittest.main()
