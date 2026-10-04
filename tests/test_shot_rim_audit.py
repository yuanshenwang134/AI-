import json
from pathlib import Path
import tempfile
import unittest
from aihoop.legacy_shots.stream import LegacyShotStream, replay_trace
from aihoop.legacy_shots.detector import Det
from aihoop.pipeline import _evidence_meta
from aihoop.sources import RawTrack

def rim(x=100,conf=.9):return Det('rim',conf,(x-20,95,x+20,105))
def ball(y):return Det('basketball',.9,(95,y-5,105,y+5))

class RimAudit(unittest.TestCase):
    def test_records_all_candidates_and_reasons_without_changing_selection(self):
        s=LegacyShotStream(30,640,hint=(100,100))
        candidates=[rim(),rim(300),rim(100,.3),Det('rim',.9,(95,90,105,110))]
        for i in range(2):s.update(i,i/30,candidates)
        row=s.frames[-1]
        self.assertEqual(len(row['rim_candidates']),4)
        self.assertEqual([r['selection_reason'] for r in row['rim_candidates']],
                         ['selected','not_nearest_hint','low_confidence','invalid_shape'])
        self.assertEqual(row['tracker_reason'],'initialized')
        self.assertEqual(row['tracked_rim']['cx'],100)
        self.assertTrue(row['hint_relation']['inside_box'])
        self.assertEqual(replay_trace(s.to_dict()),s.events)

    def test_jump_rejected_candidate_is_not_actual_rim(self):
        s=LegacyShotStream(30,640)
        s.update(0,0,[rim()]);s.update(1,.03,[rim()])
        s.update(2,.06,[rim(400)])
        row=s.frames[-1]
        self.assertEqual(row['rim']['xyxy'][0],380)
        self.assertEqual(row['tracked_rim']['cx'],100)
        self.assertEqual(row['rim_candidates'][0]['tracker_reason'],'jump_rejected')
        s.update(60,2,[])
        self.assertFalse(s.frames[-1]['tracked_rim']['fresh'])
        self.assertEqual(s.frames[-1]['tracker_reason'],'no_candidate')

    def test_rejected_candidates_and_frames_have_distinct_units(self):
        s=LegacyShotStream(30,640,hint=(100,100),config={'detect':{'center_lock_widths':3}})
        s.update(0,0,[rim()]);s.update(1,.03,[rim()])
        s.update(2,.06,[rim(400),rim(500)])
        d=s.tracking_summary()
        self.assertEqual((d['rejected_candidates'],d['rejected_frames']),(2,1))
        self.assertEqual([c['selection_reason'] for c in s.frames[-1]['rim_candidates']],['outside_center_lock']*2)

    def test_reacquisition_closes_old_attempt_and_clears_evidence(self):
        s=LegacyShotStream(30,640)
        s.update(0,0,[rim()]);s.update(1,.03,[rim()])
        s.update(2,.06,[rim(),ball(80)])
        self.assertIsNotNone(s.engine.attempt)
        for i in range(50,56):s.update(i,i/30,[rim(400)])
        self.assertEqual(s.engine.rim.last_update_reason,'reacquired_far')
        self.assertEqual([e['type'] for e in s.events],['uncertain'])
        self.assertIsNone(s.engine.attempt)
        self.assertEqual(list(s.engine.recent_real),[])
        self.assertEqual(list(s.engine.history),[])
        self.assertEqual(list(s.engine.ball_hist),[])
        tr=s.transitions[-1]
        self.assertEqual((tr['previous']['segment'],tr['current']['segment']),(1,2))
        self.assertEqual(tr['closed_attempts'],1)
        self.assertEqual(s.frames[-1]['evidence_segment'],1)
        self.assertEqual(replay_trace(s.to_dict()),s.events)

    def test_smooth_motion_does_not_create_new_identity(self):
        s=LegacyShotStream(30,640)
        for i in range(20):s.update(i,i/30,[rim(100+i)])
        self.assertEqual(len(s.transitions),1)
        self.assertEqual(s.frames[-1]['tracked_rim']['segment'],1)

    def test_cut_is_logged_and_new_segment_separate(self):
        s=LegacyShotStream(30,640)
        s.update(0,0,[rim()]);s.update(1,.03,[rim()]);s.update(2,.06,[rim(),ball(80)])
        s.update(3,.1,[rim(400)],cut=True);s.update(4,.13,[rim(400)])
        self.assertEqual([t['reason'] for t in s.transitions],['initialized','cut','initialized'])
        self.assertEqual(s.transitions[1]['closed_attempts'],1)
        self.assertEqual(s.track_segment,2)
        self.assertEqual(replay_trace(s.to_dict()),s.events)

    def test_manual_hoop_is_distinguishable_from_detector(self):
        from aihoop.hoop import Hoop
        s=LegacyShotStream(30,640,manual_hoop=Hoop(100,100,20,5))
        s.update(0,0,[rim(400)])
        self.assertEqual(s.frames[0]['selected_rim_source'],'manual')
        self.assertEqual(s.frames[0]['rim_candidates'][0]['selection_reason'],'manual_override')

    def test_raw_save_and_product_metadata_contain_diagnostics(self):
        s=LegacyShotStream(30,640,hint=(110,100))
        s.update(0,0,[rim()]);s.update(1,.03,[rim()])
        rt=RawTrack(detections_meta={'legacy_shots':s.to_dict(),'shot_engine':'legacy'})
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'raw_track.json';rt.save(str(path))
            saved=json.loads(path.read_text(encoding='utf-8'))['detections_meta']['legacy_shots']
        self.assertEqual(saved['trace_schema'],2)
        self.assertEqual(len(saved['frames'][0]['rim_candidates']),1)
        summary=_evidence_meta(rt)['shot_engine_details']
        self.assertEqual(summary['rim_tracking']['first_confirmed']['tracked_rim']['cx'],100)
        self.assertNotIn('frames',summary)

if __name__=='__main__':unittest.main()
