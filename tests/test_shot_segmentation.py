import importlib.util
from pathlib import Path
from types import SimpleNamespace
import unittest
from aihoop.hoop import _arc_cuts, _shot_segments, Hoop, HoopTrack, detect_shots
from test_shot_visibility import arc


def script(name):
    path=Path(__file__).resolve().parents[1]/'scripts'/f'{name}.py'
    spec=importlib.util.spec_from_file_location(name,path)
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    return module


class SegmentationTests(unittest.TestCase):
    def test_slow_reversal_uses_actual_extreme(self):
        points=[SimpleNamespace(y=y) for y in [0,20,24,28,32,36,40,36,32,28,24,20]]
        self.assertEqual(_arc_cuts(points,15),[6])

    def test_small_wobble_does_not_split(self):
        self.assertEqual(_arc_cuts([SimpleNamespace(y=y) for y in [0,30,60,58,62,59,80]],15),[])

    def test_arc_splits_are_camera_relative(self):
        points=arc(); samples=[]
        for i,p in enumerate(points):
            dy=100 if i%4<2 else -100
            samples.append((p.t,Hoop(cx=500,cy=200+dy,rx=30,ry=10)))
            p.y+=dy
        ht=HoopTrack(samples=samples,smooth_window=0)
        self.assertEqual(len(list(_shot_segments([points],ht,.6,20))),1)

    def test_fragments_of_same_crossing_deduplicate(self):
        hoop=Hoop(cx=500,cy=200,rx=30,ry=10)
        # Whole ascent starts at 0, second detection starts at .533 seconds.
        self.assertEqual(len(detect_shots([arc(),arc()[16:]],hoop)),1)

    def test_repeated_shots_on_long_track_survive(self):
        points=[]
        for n in range(3):
            one=arc()
            for p in one:
                p.t+=n*1.1
            points.extend(one)
        shots=detect_shots([points],Hoop(cx=500,cy=200,rx=30,ry=10))
        self.assertEqual(sum(s.made is True for s in shots),3)

    def test_equal_counts_do_not_prove_alignment(self):
        result=script('eval_shots_vs_truth').summarize(['make','unknown'],[{'made':True},{'made':None}])
        self.assertFalse(result['aligned'])
        self.assertIsNone(result['per_shot'])
        self.assertNotIn('per_shot_ok',result)

    def test_review_export_preserves_unknown_and_clip_bounds(self):
        rows=script('export_shot_review').review_rows({'duration':10,'attempts':[{'t':1,'made':None},{'t':9,'made':False}]})
        self.assertEqual(rows[0]['clip_start'],0)
        self.assertEqual(rows[1]['clip_end'],10)
        self.assertEqual(rows[0]['model_result'],'unknown')
        self.assertEqual(rows[0]['result'],'')


if __name__=='__main__':
    unittest.main()
