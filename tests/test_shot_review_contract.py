import csv
import io
import tempfile
import unittest
from unittest.mock import patch
from aihoop.sources import Attempt, RawTrack
from aihoop.pipeline import run_pipeline, PipelineConfig
from aihoop.export import shots_to_csv_string
from aihoop.rules import build_shots

class ReviewContractTests(unittest.TestCase):
    def test_unknown_is_null_and_csv_blank(self):
        shot = build_shots([Attempt(t=1, team='home', player_id='H1', x=0, y=0).to_dict()])[0]
        self.assertIsNone(shot.to_dict()['made'])
        self.assertEqual(list(csv.DictReader(io.StringIO(shots_to_csv_string([shot]))))[0]['made'], '')

    def test_sorted_review_indices_match_timeline(self):
        rt = RawTrack(duration=20)
        rt.attempts = [Attempt(t=t, team='home', player_id='H1', x=0, y=0) for t in [10.1234, 1.2345]]
        with tempfile.TemporaryDirectory() as d:
            result = run_pipeline(rt, PipelineConfig(out_dir=d, make_highlights=False, make_tactics=False))
        for i, row in enumerate(result.game['needs_review']):
            event = result.game['timeline'][row['index']]
            self.assertEqual(row['index'], i)
            self.assertEqual(event['index'], i)
            self.assertAlmostEqual(row['t'], event['t'], places=2)
            self.assertIsNone(row['made'])
            self.assertIsNone(event['made'])

    def test_invalid_value_rejected_before_read_or_write(self):
        from fastapi.testclient import TestClient
        from aihoop import api
        client = TestClient(api.app)  # No lifespan: never run real job recovery in this unit test.
        with patch.object(api, '_load_json') as load:
            for value in [0, 4, 9, -1]:
                response = client.post('/api/games/fixture/shots/0/correct', json={'made':True, 'value':value})
                self.assertEqual(response.status_code, 400)
            load.assert_not_called()
        client.close()

if __name__ == '__main__': unittest.main()
