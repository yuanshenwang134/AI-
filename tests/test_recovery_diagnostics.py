import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from aihoop.job_recovery import recover_jobs, process_identity, owner_alive
from aihoop.hoop import Hoop, HoopConfig
from aihoop.hoopsight import SightConfig, _blobs_in_window, _shots_from_blob_series


class RecoveryTests(unittest.TestCase):
    def test_dead_only_and_idempotent(self):
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'jobs.sqlite'
            with closing(sqlite3.connect(path)) as c, c:
                c.execute('CREATE TABLE jobs(job_id TEXT,status TEXT,payload TEXT,message TEXT,error TEXT,updated REAL)')
                for name,state,owner in [('dead','running','dead'),('queued','queued','dead'),
                                         ('live','running','live'),('legacy','running',None),
                                         ('complete','done','dead'),('unknown','running','unknown')]:
                    c.execute('INSERT INTO jobs VALUES(?,?,?,?,?,?)',(name,state,json.dumps({'_worker_owner':owner}),'',None,0))
            alive=lambda owner: {'dead':False,'live':True}.get(owner)
            self.assertEqual(recover_jobs(path,alive),['dead','queued'])
            self.assertEqual(recover_jobs(path,alive),[])
            with closing(sqlite3.connect(path)) as c, c:
                rows={r[0]:r[1:] for r in c.execute('SELECT job_id,status,message FROM jobs')}
            self.assertEqual(rows['live'][0],'running')
            self.assertEqual(rows['unknown'][0],'running')
            self.assertEqual(rows['complete'][0],'done')
            self.assertIn('待确认',rows['legacy'][1])

    def test_live_process_identity(self):
        identity=process_identity()
        if identity:
            self.assertTrue(owner_alive(identity))
            self.assertFalse(owner_alive({**identity,'started':identity['started']-100}))


class TraceTests(unittest.TestCase):
    def test_strict_interpolation_default_stays_off(self):
        self.assertFalse(HoopConfig().allow_hole_interpolation)

    def test_empty_motion_and_size_filter_are_distinguishable(self):
        import cv2
        import numpy as np
        cfg=SightConfig(motion_open=0); hoop=Hoop(cx=50,cy=50,rx=30,ry=10)
        previous=np.zeros((100,100),dtype=np.uint8); frame=previous.copy()
        trace={}
        self.assertEqual(_blobs_in_window(cv2,np,previous,frame,(0,0,100,100),hoop,cfg,trace=trace),[])
        self.assertEqual(trace['stage'],'no_motion_components')
        frame[40:42,40:42]=255
        _blobs_in_window(cv2,np,previous,frame,(0,0,100,100),hoop,cfg,trace=trace)
        self.assertEqual(trace['rejected']['area_small'],1)
        self.assertEqual(trace['stage'],'all_components_filtered')

    def test_trace_does_not_change_geometry(self):
        cfg=SightConfig(); hoop=Hoop(cx=100,cy=60,rx=40,ry=18)
        series=[(i/30,100,40+10*i,500) for i in range(6)]
        trace={'counts':{},'spans':[],'spans_truncated':0}
        before=_shots_from_blob_series(series,hoop,cfg)
        after=_shots_from_blob_series(series,hoop,cfg,diagnostics=trace)
        self.assertEqual([s.to_dict() for s in before],[s.to_dict() for s in after])
        self.assertTrue(trace['spans'])

    def test_trace_limit_and_no_entry(self):
        cfg=SightConfig(max_frame_trace=0); hoop=Hoop(cx=100,cy=60,rx=40,ry=18)
        trace={'counts':{},'spans':[],'spans_truncated':0}
        _shots_from_blob_series([(0,100,120,500)],hoop,cfg,diagnostics=trace)
        self.assertEqual(trace['counts']['no_entry_in_band'],1)
        self.assertEqual(trace['spans'],[])
        self.assertEqual(trace['spans_truncated'],1)

    def test_entry_without_exit(self):
        trace={'counts':{},'spans':[],'spans_truncated':0}
        _shots_from_blob_series([(0,100,40,500)],Hoop(cx=100,cy=60,rx=40,ry=18),SightConfig(),diagnostics=trace)
        self.assertEqual(trace['spans'][0]['reason'],'no_exit_in_band')


if __name__=='__main__':
    unittest.main()
