import unittest
from aihoop.legacy_shots.stream import rim_availability,LegacyShotStream
from aihoop.sources import RawTrack
from aihoop.pipeline import _evidence_meta

def rows(states):
 return [dict(t=i,tracked_rim={'fresh':v} if v is not None else None) for i,v in enumerate(states)]
class Availability(unittest.TestCase):
 def test_empty_and_old_data_have_no_fabricated_summary(self):
  self.assertIsNone(rim_availability([],30))
  self.assertIsNone(rim_availability([{'t':0}],30))
 def test_stale_and_missing_are_unavailable(self):
  a=rim_availability(rows([True,False,None,True]),1)
  self.assertEqual(a['usable_frames'],2)
  self.assertEqual(a['usable_fraction'],.5)
  self.assertEqual(a['unavailable_ranges'],[{'start':1.,'end':3.}])
 def test_trailing_gap_includes_last_frame_duration(self):
  a=rim_availability(rows([True,False,False]),1)
  self.assertEqual(a['unavailable_ranges'],[{'start':1.,'end':3.}])
 def test_short_gap_not_displayed_but_counts_in_fraction(self):
  a=rim_availability(rows([True,False,True]),1)
  self.assertEqual(a['unavailable_ranges'],[])
  self.assertAlmostEqual(a['usable_fraction'],2/3)
 def test_all_unavailable_is_not_zero_shots_claim(self):
  a=rim_availability(rows([None]*3),1)
  self.assertEqual(a['usable_frames'],0)
  self.assertEqual(a['unavailable_ranges'],[{'start':0.,'end':3.}])
 def test_summary_reaches_game_metadata_without_frame_dump(self):
  s=LegacyShotStream(1,640);s.frames=rows([False]*3)
  rt=RawTrack(detections_meta={'legacy_shots':s.to_dict()})
  meta=_evidence_meta(rt)['shot_engine_details']
  self.assertEqual(meta['rim_tracking']['availability']['usable_frames'],0)
  self.assertNotIn('frames',meta)

if __name__=='__main__':unittest.main()
