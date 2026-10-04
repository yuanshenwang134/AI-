import unittest
from aihoop.legacy_shots.scorer import ScoreEngine
from aihoop.legacy_shots.stream import DEFAULT_CONFIG
from aihoop.legacy_shots.detector import Det
from aihoop.legacy_shots.adapter import to_attempts
from aihoop.sources import RawTrack
from aihoop.rules import build_shots
from aihoop.pipeline import _shot_event

RIM=Det('rim',.9,(80,95,120,105))
def ball(x,y,predicted=False):return Det('basketball',.9,(x-5,y-5,x+5,y+5),predicted=predicted)
def sequence(x=100,gap=.533,second=True,predicted=False):
 e=ScoreEngine(DEFAULT_CONFIG,30)
 e.update(0,0,None,RIM,[]);e.update(1,.033,None,RIM,[])
 e.update(3,.1,ball(x,90),RIM,[])
 e.update(19,.1+gap,ball(100,120),RIM,[])
 if second:e.update(20,.133+gap,ball(100,130,predicted),RIM,[])
 e.finalize(21,.167+gap)
 return e.events

class GapSuggestions(unittest.TestCase):
 def test_only_unknown_suggestion_with_separate_review_time(self):
  rows=sequence();self.assertEqual(len(rows),1)
  row=rows[0]
  self.assertEqual(row['type'],'uncertain');self.assertIsNone(row['made'])
  self.assertTrue(row['suggested_made']);self.assertIsNone(row['crossing_t'])
  self.assertAlmostEqual(row['review_t'],.633)
  self.assertEqual(row['points'],0)
 def test_wide_approach_is_not_valid_aperture(self):
  self.assertIsNone(sequence(x=130)[0]['suggested_made'])
 def test_gap_upper_bound(self):
  self.assertIsNone(sequence(gap=.8)[0]['suggested_made'])
 def test_one_observation_is_insufficient(self):
  self.assertIsNone(sequence(second=False)[0]['suggested_made'])
 def test_predicted_exit_does_not_count(self):
  self.assertIsNone(sequence(predicted=True)[0]['suggested_made'])
 def test_measured_crossing_not_relabelled(self):
  rows=sequence(gap=.2)
  self.assertEqual(rows[0]['type'],'make')
  self.assertIsNone(rows[0]['suggested_made']);self.assertIsNone(rows[0]['review_t'])
 def test_reacquisition_breaks_suggestion_chain(self):
  e=ScoreEngine(DEFAULT_CONFIG,30)
  e.update(0,0,None,RIM,[]);e.update(1,.033,None,RIM,[])
  e.update(3,.1,ball(100,90),RIM,[])
  e.update(60,2,ball(100,120),RIM,[])
  e.update(61,2.033,ball(100,130),RIM,[])
  e.finalize(62,2.067)
  self.assertTrue(e.events)
  self.assertFalse(any(r['suggested_made'] for r in e.events))
 def test_timestamp_and_no_score_survive_product_pipeline(self):
  rt=RawTrack(fps=30,duration=5)
  a=to_attempts(sequence(),rt)[0]
  shot=build_shots([a.to_dict()],[],[])[0]
  row=_shot_event(shot,rt)
  self.assertEqual(row['review_t'],.633)
  self.assertIsNone(row['crossing_t']);self.assertTrue(row['suggested_made'])
  self.assertEqual(shot.result,'unknown');self.assertFalse(shot.counts_for_score)
  self.assertEqual(shot.points,0);self.assertEqual(shot.score_points,0)
  self.assertEqual(shot.evidence,'legacy_occlusion_descent')

if __name__=='__main__':unittest.main()
