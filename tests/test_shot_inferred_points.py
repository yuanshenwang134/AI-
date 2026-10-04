import importlib.util
from pathlib import Path
import unittest
from aihoop.hoop import Hoop, detect_shots
from test_shot_visibility import arc

class InferredPointTests(unittest.TestCase):
    def test_synthetic_points_never_witness_a_make(self):
        for source in ['interp','interpolated','inpaint','inpainted','kalman','predicted']:
            points=arc()
            for p in points: p.source=source
            self.assertEqual(detect_shots([points],Hoop(500,200,30,10)),[])

    def test_measured_arc_still_works(self):
        self.assertTrue(any(s.made is True for s in detect_shots([arc()],Hoop(500,200,30,10))))

    def test_interpolated_exit_does_not_become_observed(self):
        points=arc()
        for p in points:
            if p.y >= 200: p.source='interp'
        self.assertFalse(any(s.made is True for s in detect_shots([points],Hoop(500,200,30,10))))

    def test_review_export_keeps_suggestion_and_covers_crossing(self):
        script=Path(__file__).resolve().parents[1]/'scripts/export_shot_review.py'
        spec=importlib.util.spec_from_file_location('review_export',script)
        module=importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
        row=module.review_rows({'duration':30,'attempts':[{'t':1,'made':None,
          'crossing_t':10,'suggested_made':True,'evidence':'cross_extrapolated',
          'tags':['needs_review']}]})[0]
        self.assertEqual(row['model_result'],'unknown')
        self.assertTrue(row['suggested_made'])
        self.assertEqual(row['clip_end'],14)
        self.assertTrue(row['needs_review'])

if __name__=='__main__': unittest.main()
