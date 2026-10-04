"""Regression checks for uncertain outcomes and one-use evidence."""
import csv
import io
import unittest
from unittest.mock import patch
from aihoop.rules import (Evidence, RimHit, build_shots, fuse_outcome,
                          compute_team_stats, compute_player_stats)
from aihoop.export import shots_to_csv_string, write_shots_csv
from tempfile import TemporaryDirectory
from pathlib import Path

def attempt(t=1.0, **changes):
    return dict(t=t, team="home", player_id="P1", x=0, y=1.575, **changes)

class OutcomeTests(unittest.TestCase):
    def test_empty_zero_and_tied_evidence_are_unknown(self):
        cases = [[], [Evidence("synthetic", True, 1, 0)],
                 [Evidence("synthetic", True, 1), Evidence("synthetic", False, 1)]]
        for evidence in cases:
            self.assertEqual(fuse_outcome(evidence)[:2], (None, 0.0))

    def test_explicit_results_and_unknown_statistics(self):
        shots = build_shots([attempt(1, made=True), attempt(2, made=False), attempt(3, made=None)])
        self.assertEqual([s.result for s in shots], ["made", "missed", "unknown"])
        self.assertIn("needs_review", shots[-1].tags)
        stats = compute_team_stats(shots, "home")
        self.assertEqual((stats.fgm, stats.fga, stats.unknown, stats.fg_pct), (1, 2, 1, .5))
        player = compute_player_stats(shots)[0]
        self.assertEqual((player["fga"], player["unknown"], player["fg_pct"]), (2, 1, .5))

    def test_scoreboard_used_once_for_nearest_attempt(self):
        shots = build_shots([attempt(1), attempt(2)], scoreboard_events=[dict(t=3, team="home", delta=2)])
        self.assertEqual([s.result for s in shots], ["unknown", "made"])

    def test_scoreboard_wrong_team_value_or_time_cannot_confirm(self):
        for event in [dict(t=3, team="away", delta=2), dict(t=3, team="home", delta=4),
                      dict(t=.5, team="home", delta=2), dict(t=8, team="home", delta=2)]:
            self.assertEqual(build_shots([attempt()], scoreboard_events=[event])[0].result, "unknown")

    def test_rim_hit_used_once_and_never_before_attempt(self):
        with patch("aihoop.rules.detect_rim_events", return_value=[RimHit(2.5, True)]):
            shots = build_shots([attempt(1), attempt(2), attempt(3)], ball_track=[object()])
        self.assertEqual([s.result for s in shots], ["unknown", "made", "unknown"])

    def test_highlight_preserves_unknown(self):
        from aihoop.highlight import make_highlights
        with TemporaryDirectory() as temp:
            clips = make_highlights(build_shots([attempt()]), None, temp)
        self.assertEqual(clips[0]["result"], "unknown")
        self.assertIn("待确认", clips[0]["label"])

    def test_csv_preserves_unknown_separately_from_miss(self):
        shots = build_shots([attempt(), attempt(2, made=False)])
        rows = list(csv.DictReader(io.StringIO(shots_to_csv_string(shots))))
        self.assertEqual([(r["made"], r["result"]) for r in rows], [("", "unknown"), ("0", "missed")])
        with TemporaryDirectory() as temp:
            path=Path(temp)/"shots.csv"
            write_shots_csv(str(path), shots)
            with path.open(encoding="utf-8-sig", newline="") as f:
                rows=list(csv.DictReader(f))
            self.assertEqual(rows[0]["result"], "unknown")
            self.assertEqual(rows[0]["made"], "")

if __name__ == "__main__":
    unittest.main()
