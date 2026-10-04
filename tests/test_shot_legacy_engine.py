import copy
import unittest
import sys
from types import SimpleNamespace
from unittest.mock import patch
from aihoop.legacy_shots.stream import LegacyShotStream, replay_trace
from aihoop.legacy_shots.detector import Det
from aihoop.legacy_shots.adapter import to_attempts
from aihoop.sources import RawTrack, VideoSource
from aihoop.model import Player
from aihoop.court import Calibration
from aihoop.hoop import Hoop
from aihoop.rules import build_shots, compute_team_stats

RIM=Det('rim',.9,(80,95,120,105))
def ball(x,y):return Det('basketball',.9,(x-5,y-5,x+5,y+5))
def path(xs=None,config=None):
    stream=LegacyShotStream(30,640,config=config)
    for i in range(2):stream.update(i,i/30,[RIM])
    for i,y in enumerate((60,70,80,90,100,110,120,135,150,180),2):
        stream.update(i,i/30,[RIM,ball(xs if xs is not None else 100,y)])
    stream.finish(12,12/30)
    return stream

class LegacyIntegration(unittest.TestCase):
    def test_api_defaults_and_invalid_engine(self):
        from aihoop.api import JobCreate
        from pydantic import ValidationError
        request=JobCreate(source='video',video_path='fixture.mp4')
        self.assertEqual(request.shot_engine,'legacy')
        self.assertEqual(request.score_policy,'court')
        with self.assertRaises(ValidationError):JobCreate(shot_engine='typo')

    def test_complete_crossing_and_replay(self):
        s=path()
        self.assertEqual([e['type'] for e in s.events],['make'])
        self.assertEqual(replay_trace(s.to_dict()),s.events)
        self.assertIsNotNone(s.events[0]['crossing_t'])

    def test_side_drop_is_not_make(self):
        self.assertFalse(any(e['type']=='make' for e in path(130).events))

    def test_no_ball_no_attempt(self):
        s=LegacyShotStream(30,640)
        for i in range(15):s.update(i,i/30,[RIM])
        s.finish(15,.5)
        self.assertEqual(s.events,[])

    def test_short_gap_prediction_never_confirms(self):
        s=LegacyShotStream(30,640)
        for i in range(2):s.update(i,i/30,[RIM])
        s.update(2,2/30,[RIM,ball(100,60)])
        s.update(3,3/30,[RIM,ball(100,80)])
        for i in range(4,10):s.update(i,i/30,[RIM])
        s.finish(10,10/30)
        self.assertTrue(s.frames[4]['ball']['predicted'])
        self.assertEqual([e['type'] for e in s.events],['uncertain'])
        self.assertEqual(replay_trace(s.to_dict()),s.events)

    def test_cut_breaks_evidence(self):
        s=LegacyShotStream(30,640)
        for i in range(2):s.update(i,i/30,[RIM])
        s.update(2,2/30,[RIM,ball(100,80)])
        for i,y in enumerate((120,130,150),3):s.update(i,i/30,[RIM,ball(100,y)],cut=i==3)
        s.finish(6,6/30)
        self.assertEqual([e['type'] for e in s.events],['uncertain'])
        self.assertEqual(replay_trace(s.to_dict()),s.events)

    def test_center_only_hint_still_tracks(self):
        s=LegacyShotStream(30,640,manual_hoop=Hoop(100,100,0,0),hint=(100,100))
        far=Det('rim',1.,(300,95,340,105))
        for i in range(2):s.update(i,i/30,[RIM,far])
        self.assertEqual(s.engine.rim.cx,100)
        s.update(2,2/30,[Det('rim',.9,(84,95,124,105))])
        self.assertGreater(s.engine.rim.cx,100)

    def test_manual_complete_rim_without_detector(self):
        s=LegacyShotStream(30,640,manual_hoop=Hoop(100,100,20,5))
        for i in range(2):s.update(i,i/30,[])
        self.assertEqual(s.engine.rim.cx,100)

    def test_adapter_preserves_three_states_and_suggestion(self):
        base=path().events[0]
        events=[]
        for i,kind in enumerate(('make','miss','uncertain')):
            e=copy.deepcopy(base);e.update(type=kind,release_t=float(i),player_id='T1',suggested_made=True if kind=='uncertain' else None)
            events.append(e)
        rt=RawTrack(fps=30,players={'T1':Player('T1','甲','home')})
        attempts=to_attempts(events,rt)
        shots=build_shots([a.to_dict() for a in attempts],[],[])
        self.assertEqual([s.result for s in shots],['made','missed','unknown'])
        self.assertEqual([s.to_dict()['made'] for s in shots],[True,False,None])
        self.assertEqual(shots[0].outcome_source,'ball_through_rim')
        self.assertIs(shots[2].suggested_made,True)
        self.assertEqual(sum(s.score_points for s in shots),2)
        self.assertEqual(len(shots),3)

    def test_unassigned_team_is_not_scored(self):
        a=to_attempts(path().events,RawTrack(fps=30))[0]
        self.assertIs(a.made,True)
        self.assertFalse(a.counts_for_score)
        self.assertIn('team_unknown',a.tags)
        self.assertEqual(a.location_source,'rim_placeholder')

    def test_default_dispatch_keeps_miss_and_unknown(self):
        source=VideoSource('fixture.mp4',Calibration(),detect_players=False)
        self.assertEqual(source.shot_engine,'legacy')
        source._legacy_events=path().events
        rt=RawTrack(fps=30)
        with patch.object(source,'_hoopsight_attempts',side_effect=AssertionError('must not run')),patch.object(source,'_visual_attempts',side_effect=AssertionError('must not run')):
            source._dispatch_shot_engine(rt)
        self.assertEqual(len(rt.attempts),1)
        self.assertEqual(rt.detections_meta['visual']['engine'],'legacy')

    def test_geometry_is_explicit_comparison(self):
        source=VideoSource('fixture.mp4',Calibration(),shot_engine='geometry')
        with patch.object(source,'_hoopsight_attempts') as hs,patch.object(source,'_visual_attempts') as vis:
            source._dispatch_shot_engine(RawTrack())
        hs.assert_called_once();vis.assert_called_once()
        with self.assertRaises(ValueError):VideoSource('fixture',Calibration(),shot_engine='typo')

    def test_clip_includes_late_crossing(self):
        e=copy.deepcopy(path().events[0]);e.update(release_t=1.,t=4.,crossing_t=3.8)
        a=to_attempts([e],RawTrack(fps=30,duration=10))[0]
        shot=build_shots([a.to_dict()],[],[])[0]
        self.assertGreater(shot.clip_end,4.)
        self.assertEqual(shot.t,1.)

    def test_video_source_feeds_joint_detections_without_gpu(self):
        import cv2
        import numpy as np
        from aihoop.ball import BallConfig
        image=np.zeros((360,640,3),dtype=np.uint8)
        class Capture:
            index=-1
            def isOpened(self):return True
            def get(self,key):return {cv2.CAP_PROP_FPS:30,cv2.CAP_PROP_FRAME_COUNT:12,
                cv2.CAP_PROP_FRAME_WIDTH:640,cv2.CAP_PROP_FRAME_HEIGHT:360}.get(key,0)
            def grab(self):self.index+=1;return self.index<12
            def retrieve(self):return True,image.copy()
            def release(self):pass
        seen=[]
        class Model:
            index=0
            def predict(self,frame,**options):
                seen.append(options)
                boxes=[SimpleNamespace(cls=[1],conf=[.9],xyxy=[RIM.xyxy])]
                if self.index>=2:
                    y=(60,70,80,90,100,110,120,135,150,180)[self.index-2]
                    boxes.append(SimpleNamespace(cls=[0],conf=[.9],xyxy=[ball(100,y).xyxy]))
                self.index+=1
                return [SimpleNamespace(boxes=boxes,names={0:'basketball',1:'rim'})]
        source=VideoSource('fixture.mp4',Calibration(),detect_players=False,auto_sliding=False,
                           scoreboard=False,ball_weights='fixture.pt',ball_cfg=BallConfig(use_bg=False))
        with patch.object(source,'_require'),patch('cv2.VideoCapture',side_effect=lambda *a:Capture()),\
             patch.dict(sys.modules,{'ultralytics':SimpleNamespace(YOLO=lambda *a:Model())}),\
             patch('aihoop.calibcheck.camera_motion',return_value={'verdict':'static'}),\
             patch.object(source,'_calibration_matches',return_value=False),\
             patch.object(source,'_hoopsight_attempts',side_effect=AssertionError('second engine')),\
             patch.object(source,'_visual_attempts',side_effect=AssertionError('second engine')),\
             patch.object(source,'extract_attempts',side_effect=AssertionError('duplicate candidates')):
            rt=source.run()
        self.assertEqual(len(seen),12)
        self.assertTrue(all(o['imgsz']==640 and o['iou']==.5 for o in seen))
        self.assertEqual([a.made for a in rt.attempts],[True])
        self.assertEqual(rt.detections_meta['shot_engine'],'legacy')
        trace=rt.detections_meta['legacy_shots']
        self.assertEqual(replay_trace(trace),trace['events'])
        self.assertEqual(trace['trace_schema'],2)
        self.assertTrue(all('rim_candidates' in row and 'tracked_rim' in row for row in trace['frames']))
        self.assertEqual(trace['rim_tracking']['first_confirmed']['tracked_rim']['cx'],100)

if __name__=='__main__':unittest.main(verbosity=2)
