import importlib.util
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from aihoop.legacy_shots.scorer import ScoreEngine, Attempt
from aihoop.legacy_shots.stream import LegacyShotStream, DEFAULT_CONFIG
from aihoop.legacy_shots.detector import Det
from aihoop.legacy_shots.adapter import to_attempts
from aihoop.sources import RawTrack
from aihoop.rules import build_shots, shot_chart
from aihoop.pipeline import _shot_event
from aihoop.highlight import make_highlights
from aihoop.model import Shot

RIM=Det('rim',.9,(80,95,120,105))
ROOT=Path(__file__).resolve().parents[1]

class ReviewFixes(unittest.TestCase):
    def test_clip_reaches_actual_cut_command_and_timeline(self):
        rt=RawTrack(fps=30,duration=10)
        event=dict(type='uncertain',release_t=1.,t=4.,release_source='ball',crossing_t=None)
        a=to_attempts([event],rt)[0]
        shot=build_shots([a.to_dict()],[],[])[0]
        row=_shot_event(shot,rt)
        self.assertEqual(row['decision_t'],4.)
        self.assertEqual(row['release_source'],'ball')
        self.assertEqual(row['clip_end'],5.5)
        with tempfile.TemporaryDirectory() as folder:
            video=Path(folder)/'video.mp4';video.touch()
            with patch('aihoop.highlight.ffmpeg_path',return_value='ffmpeg'),patch('aihoop.highlight._cut',return_value=False) as cut:
                clips=make_highlights([shot],str(video),folder,duration=5)
            self.assertEqual(cut.call_args.args[2:],(shot.clip_start,5))
            self.assertEqual(clips[0]['end'],5)

    def test_old_clip_defaults_fallback(self):
        shot=Shot(5,'home','T1')
        with tempfile.TemporaryDirectory() as folder:
            clips=make_highlights([shot],None,folder)
        self.assertGreater(clips[0]['end'],5)
        self.assertLess(clips[0]['start'],5)

    def test_placeholder_excluded_from_chart_but_kept_in_timeline(self):
        shot=Shot(1,'home','T1',made=True,result='made',tags=['location_unknown','value_assumed'])
        self.assertEqual(shot_chart([shot])['points'],[])
        self.assertIsNone(shot.to_dict()['distance'])
        row=_shot_event(shot,RawTrack())
        self.assertEqual(row['zone'],'位置未知')
        self.assertTrue(row['value_assumed'])
        self.assertTrue(row['made'])

    def prepared(self,crossing=None):
        e=ScoreEngine(DEFAULT_CONFIG,30)
        e.update(0,3.7,None,RIM,[]);e.update(1,3.8,None,RIM,[])
        e.attempt=Attempt(start_frame=2,start_t=0.,release_xy=(100,80),dist_est_m=1.,player_id=None)
        e.attempt.entered=True;e.attempt.crossing_t=crossing
        e.attempt.last_real_t=3.8
        return e

    def test_occluded_timeout_real_predicted_and_missing_are_unknown(self):
        for ball in [Det('basketball',.9,(95,180,105,190)),Det('basketball',0,(95,180,105,190),predicted=True),None]:
            e=self.prepared()
            rows=e.update(120,4.,ball,RIM,[])
            self.assertEqual([r['type'] for r in rows],['uncertain'])
            self.assertIsNone(rows[0]['suggested_made'])

    def test_observed_crossing_not_blanket_changed_to_unknown(self):
        e=self.prepared(crossing=1.)
        rows=e.update(120,4.,Det('basketball',.9,(195,180,205,190)),RIM,[])
        self.assertEqual(rows[0]['type'],'miss')

    def test_center_lock_rejects_other_hoop_after_stale_and_cut(self):
        s=LegacyShotStream(30,640,config={'detect':{'center_lock_widths':3}},hint=(100,100))
        for i in range(3):s.update(i,i/30,[RIM])
        far=Det('rim',1,(380,95,420,105))
        for i in range(3,150):s.update(i,i/30,[far],cut=i==80)
        self.assertTrue(all(row['rim'] is None for row in s.frames[3:]))
        self.assertEqual(s.center_rejected,147)
        self.assertEqual(s.center_lock_width,40)
        s.update(150,5,[RIM]);s.update(151,5.03,[RIM])
        self.assertEqual(s.engine.rim.cx,100)

    def test_center_lock_allows_local_motion_and_default_off(self):
        self.assertEqual(DEFAULT_CONFIG['detect']['center_lock_widths'],0)
        s=LegacyShotStream(30,640,config={'detect':{'center_lock_widths':3}},hint=(100,100))
        for i in range(3):s.update(i,i/30,[RIM])
        s.update(3,.1,[Det('rim',.9,(84,95,124,105))])
        self.assertGreater(s.engine.rim.cx,100)

    def test_engine_fallback_matches_product(self):
        e=ScoreEngine({},30)
        self.assertEqual(e.miss_timeout_s,DEFAULT_CONFIG['score']['miss_timeout_s'])
        self.assertEqual(e.rim_readopt_far_frames,DEFAULT_CONFIG['score']['rim_readopt_far_frames'])

    def test_reload_watches_nested_files_and_waits_before_stop(self):
        spec=importlib.util.spec_from_file_location('reload_test',ROOT/'scripts/serve_reload.py')
        mod=importlib.util.module_from_spec(spec);spec.loader.exec_module(mod)
        with tempfile.TemporaryDirectory() as folder:
            nested=Path(folder)/'legacy_shots';nested.mkdir();file=nested/'scorer.py';file.touch()
            with patch.object(mod,'WATCH_DIR',Path(folder)):
                self.assertIn(str(file),mod.snapshot())
        from unittest.mock import MagicMock
        proc=MagicMock();proc.poll.side_effect=[None,None,0];proc.returncode=0
        with patch.object(mod.subprocess,'Popen',return_value=proc),patch.object(mod,'snapshot',side_effect=[{}, {'changed':1},{'changed':1}]),patch.object(mod,'jobs_running',return_value=True) as busy,patch.object(mod.time,'sleep'),patch.object(mod,'stop') as stop:
            mod.main(['8000'])
        self.assertEqual(busy.call_count,2)
        stop.assert_called_once_with(proc)

if __name__=='__main__':unittest.main()

