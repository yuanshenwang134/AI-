"""Targeted regression tests. No real video, OCR engine, or model inference."""
import importlib.util
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch, MagicMock

from aihoop.score_text import parse_number, parse_scores, build_events
from aihoop.footage import coverage_verdict
from aihoop.model import Player, Shot
from aihoop.rules import compute_player_stats
from aihoop.sources import RawTrack, VideoSource
from aihoop.pipeline import PipelineConfig, run_pipeline, _team_name
from aihoop.court import Calibration

ROOT=Path(__file__).resolve().parents[1]

def load_script(name):
    spec=importlib.util.spec_from_file_location(name,ROOT/'scripts'/f'{name}.py')
    mod=importlib.util.module_from_spec(spec);spec.loader.exec_module(mod)
    return mod

def sequence(texts,start=None):
    items=[dict(t=i,team='all',file=str(i)) for i in range(len(texts))]
    return build_events(items,{str(i):v for i,v in enumerate(texts)},start)

class ScoreTextTests(unittest.TestCase):
    def test_period_and_missing_score(self):
        self.assertIsNone(parse_scores('FHS AHS 31 3 rd Qtr')[0])
        self.assertEqual(parse_scores('FHS 37 AHS 36 3 rd Qtr 1:23')[0],{'home':37,'away':36})
        self.assertIsNone(parse_number('DoubIeACS'))
        self.assertIsNone(parse_number('3 rd Qtr'))
        self.assertEqual(parse_scores('31:31')[0],{'home':31,'away':31})

    def test_configured_labels_follow_names_not_order(self):
        self.assertEqual(parse_scores('AHS 36 FHS 37 3rd Qtr',{'home':'FHS','away':'AHS'})[0],{'home':37,'away':36})

    def test_video_baseline_and_ten_increments(self):
        scores=[(31,31),(33,31),(33,33),(35,33),(35,34),(37,34),
                (37,35),(38,35),(38,36),(38,37),(38,38)]
        lines=[f'FHS {h} AHS {a} 3 rd Qtr' for h,a in scores]
        for start in ({},{'home':31,'away':31}):
            result=sequence(lines,start)
            self.assertEqual(result['final'],{'home':38,'away':38})
            self.assertEqual(len(result['events']),10)
            self.assertEqual(result['start'],{'home':31,'away':31})
            self.assertEqual(sum(e['delta'] for e in result['events']),14)
            self.assertEqual(result['baseline_from'],'provided' if start else 'video')

    def test_incomplete_first_frame_never_creates_quarter_event(self):
        r=sequence(['FHS AHS 31 3 rd Qtr','FHS 37 AHS 36 3 rd Qtr','FHS 38 AHS 38 3 rd Qtr'])
        self.assertEqual(r['start'],{'home':37,'away':36})
        self.assertEqual(r['final'],{'home':38,'away':38})
        self.assertEqual([e['delta'] for e in r['events']],[1,2])

    def test_joint_rejection_and_large_gap_remain_strict(self):
        r=sequence(['FHS 31 AHS 31','FHS 32 AHS 2','FHS 37 AHS 36'])
        self.assertEqual(r['final'],{'home':31,'away':31})
        self.assertEqual(r['events'],[])
        r=sequence(['FHS 31 AHS 31'],{'home':0,'away':0})
        self.assertEqual(len(r['warnings']),2)

    def test_unlabelled_reading_is_marked_lower_confidence(self):
        r=sequence(['31 31','32 31'])
        self.assertEqual(r['readings'][0]['parse_mode'],'position_fallback')
        self.assertEqual(r['readings'][0]['confidence'],.5)

class IntegrationTests(unittest.TestCase):
    def test_visual_events_share_canonical_player_teams(self):
        from aihoop.hoop import ShotEvent,Hoop,HoopTrack
        players={p:Player(player_id=p,name=p,team=t) for p,t in [('P1','away'),('P2','away'),('P3','home')]}
        rt=RawTrack(players=players);rt.detections_meta['calibration_valid']=False
        source=VideoSource('stub.mp4',Calibration(),detect_players=False)
        source._tracks_px=[[object()]]
        hoop=HoopTrack(samples=[(0,Hoop(cx=100,cy=100,rx=30,ry=10))])
        shots=[ShotEvent(t=i+1,release_x=0,release_y=0,made=True,apex_y=0,confidence=.8) for i in range(3)]
        with patch('aihoop.hoop.detect_hoop_track',return_value=hoop), \
             patch('aihoop.hoop.detect_shots',return_value=shots), \
             patch('aihoop.ball.rank_ball_tracks',return_value=source._tracks_px), \
             patch.object(source,'_jump_shooter',return_value=None), \
             patch.object(source,'_locate_shooter',side_effect=[('P1',None),('P2',None),('P3',None)]), \
             patch.object(source,'_estimate_shot_value',return_value=(2,'visual_estimate')):
            source._visual_attempts(rt)
        self.assertEqual([a.team for a in rt.attempts],['away','away','home'])
        self.assertNotIn('team_names',rt.detections_meta)

    def test_synthetic_events_match_actor_teams(self):
        from aihoop.sources import synthetic_game
        rt=synthetic_game(seed=7,duration=180)
        for event in rt.detections_meta['events']:
            self.assertEqual(event['team'],rt.players[event['player_id']].team)

    def test_conflicting_player_team_fails(self):
        player=Player(player_id='T1',team='away',name='球员1')
        with self.assertRaisesRegex(ValueError,'队别冲突'):
            compute_player_stats([Shot(t=1,team='home',player_id='T1',made=True)],{'T1':player})

    def test_team_name_never_uses_player_alias(self):
        rt=RawTrack(players={'T1':Player(player_id='T1',team='home',name='球员860')})
        rt.detections_meta['team_names']={'home':'球员860','away':'FHS'}
        self.assertEqual(_team_name(rt,'home'),'主队')
        self.assertEqual(_team_name(rt,'away'),'FHS')

    def test_score_only_without_calibration_and_no_model(self):
        with TemporaryDirectory() as temp:
            path=Path(temp)/'scores.json'
            events=[dict(t=i+1,team=t,delta=d) for i,(t,d) in enumerate(
                [('home',2),('home',2),('home',2),('home',1),('away',2)]+[('away',1)]*5)]
            path.write_text(json.dumps(dict(start={'home':31,'away':31},events=events)))
            source=VideoSource('stub.mp4',Calibration(method='unavailable'),detect_players=False,
                               score_policy='scoreboard',scoreboard_events=str(path),ball_weights='should-not-load.pt')
            cap=MagicMock();cap.isOpened.return_value=True
            cap.get.side_effect=lambda k:{5:30,7:7200,3:1920,4:1080}.get(k,0)
            with patch('cv2.VideoCapture',return_value=cap):
                rt=source.run()
            cap.grab.assert_not_called()
            result=run_pipeline(rt,PipelineConfig(out_dir=str(Path(temp)/'out'),make_highlights=False))
            self.assertEqual(result.game['score'],{'home':38,'away':38})
            self.assertFalse(result.game['meta']['court_outputs_available'])
            self.assertEqual(sum(s.value==1 for s in result.shots),6)
            self.assertEqual(rt.ball_track,[])
            for team in ('home','away'):
                self.assertEqual(sum(p['points'] for p in result.players if p['team']==team),7)

    def test_jsonl_overrides_explicitly_rejected(self):
        import asyncio
        from aihoop import api
        for kwargs in ({'scoreboard_events':'x.json'},{'score_policy':'scoreboard'}):
            with self.assertRaises(api.HTTPException) as ctx:
                asyncio.run(api.create_job(api.JobCreate(source='jsonl',**kwargs)))
            self.assertEqual(ctx.exception.status_code,400)

    def test_no_calibration_api_score_only_exemption(self):
        from aihoop import api
        with TemporaryDirectory() as temp:
            video=Path(temp)/'fake.mp4';video.touch()
            rt=RawTrack();rt.detections_meta['source']='video'
            state=api.JobState(job_id='unit-test',source='video')
            req=api.JobCreate(source='video',video_path=str(video),score_policy='scoreboard',
                              detect_players=False,auto_sliding=False,make_highlights=False)
            result=MagicMock();result.out_dir=temp;result.summary.return_value='ok'
            with patch.dict(api._mem,{'unit-test':state}),patch.object(api,'DATA',Path(temp)), \
                 patch.object(api,'OUT_ROOT',Path(temp)),patch.object(api,'_persist'), \
                 patch.object(api,'_find_calibration_for',return_value=None), \
                 patch.object(api,'_find_marks_for',return_value=None), \
                 patch.object(api,'load_feedback',return_value=[]), \
                 patch('aihoop.sources.VideoSource') as source,patch.object(api,'run_pipeline',return_value=result):
                source.return_value.run.return_value=rt
                api._run_job('unit-test',req)
                self.assertEqual(state.status,'done',state.error)
                self.assertIsNone(source.call_args.args[1].H)

    def test_low_ocr_coverage_does_not_produce_output(self):
        mod=load_script('read_marked_scoreboard')
        with TemporaryDirectory() as temp,patch.object(mod,'ROOT',Path(temp)), \
             patch.object(mod,'grab_crops',return_value=[dict(t=0,team='all',file='a')]), \
             patch.object(mod,'run_ocr',return_value={'a':'3 rd Qtr'}):
            with self.assertRaisesRegex(ValueError,'有效读数不足'):
                mod.read_scoreboard('fake.mp4',box=(0,0,100,50))

    def test_shared_coverage_does_not_reject_on_pixel_size(self):
        self.assertTrue(coverage_verdict(80,100)['ok'])
        self.assertTrue(coverage_verdict(653,1000)['ok'])
        self.assertIsNone(coverage_verdict(0,0,'no model')['ok'])

    def test_custom_pythonpath_survives(self):
        with patch.dict(os.environ,{'PYTHONPATH':os.pathsep.join(['custom-libs','other-libs'])}):
            mod=load_script('check_all')
            self.assertIn('custom-libs',mod.ENV['PYTHONPATH'].split(os.pathsep))
            serve=load_script('serve_reload')
            with patch.object(serve.subprocess,'Popen') as run:
                run.return_value.poll.return_value=0
                run.return_value.returncode=0
                serve.main(['8019','--no-reload'])
                self.assertIn('custom-libs',run.call_args.kwargs['env']['PYTHONPATH'].split(os.pathsep))

    def test_auto_locator_requires_score_semantics(self):
        import numpy as np
        from types import SimpleNamespace
        mod=load_script('read_marked_scoreboard')
        cap=MagicMock()
        cap.get.side_effect=lambda k:{3:100,4:100,7:100}.get(k,0)
        cap.read.return_value=(True,np.zeros((100,100,3),dtype=np.uint8))
        wrong=SimpleNamespace(x=1,y=40,w=40,h=5)
        with TemporaryDirectory() as temp,patch('cv2.VideoCapture',return_value=cap), \
             patch('cv2.imwrite',return_value=True), \
             patch('aihoop.scoreboard.locate_score_bug_static',return_value=wrong), \
             patch('aihoop.scoreboard.locate_score_bug_box',return_value=wrong), \
             patch.object(mod,'run_ocr',return_value={f'candidate_1_{i}.png':'FHS 31 AHS 31 3rd Qtr' for i in range(5)}):
            self.assertEqual(mod.locate_scoreboard_ocr('fake.mp4',Path(temp)),(0,0,100,20))
        with TemporaryDirectory() as temp,patch('cv2.VideoCapture',return_value=cap), \
             patch('cv2.imwrite',return_value=True), \
             patch('aihoop.scoreboard.locate_score_bug_static',return_value=wrong), \
             patch('aihoop.scoreboard.locate_score_bug_box',return_value=wrong), \
             patch.object(mod,'run_ocr',return_value={}):
            with self.assertRaisesRegex(ValueError,'手动框选'):
                mod.locate_scoreboard_ocr('fake.mp4',Path(temp))

if __name__=='__main__':
    unittest.main()
