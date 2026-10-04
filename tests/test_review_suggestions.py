import unittest
from aihoop.sources import Attempt, RawTrack
from aihoop.rules import build_shots, compute_team_stats
from aihoop.pipeline import _shot_event

class ReviewSuggestionsTests(unittest.TestCase):
    def attempt(self):
        return Attempt(t=19.36, team='home', player_id='H1', x=0, y=0,
                       made=None, source='ball_rim', evidence='cross_extrapolated',
                       suggested_made=True, crossing_t=21.116,
                       tags=['inferred_outcome', 'outcome_unknown', 'needs_review']).to_dict()

    def test_suggestion_reaches_both_review_and_timeline_without_scoring(self):
        shot=build_shots([self.attempt()])[0]
        for row in [shot.to_dict(), _shot_event(shot, RawTrack())]:
            self.assertIs(row['suggested_made'], True)
            self.assertEqual(row['crossing_t'], 21.116)
            self.assertEqual(row['result'], 'unknown')
            self.assertEqual(row['points'], 0)
        self.assertEqual(compute_team_stats([shot], 'home').fgm, 0)

    def test_manual_accept_and_reject_clear_queue_preserving_audit(self):
        for made in [True, False]:
            a=self.attempt(); a.update(made=made, manual=True, conf=1.0)
            shot=build_shots([a])[0]
            self.assertIs(shot.made, made)
            self.assertEqual(shot.outcome_source, 'manual')
            self.assertNotIn('needs_review', shot.tags)
            self.assertNotIn('outcome_unknown', shot.tags)
            self.assertIs(shot.suggested_made, True)
            self.assertEqual(compute_team_stats([shot], 'home').fgm, int(made))

    def test_manual_miss_overrides_conflicting_scoreboard(self):
        a=self.attempt(); a.update(made=False, manual=True, conf=1.0, forced_value=2)
        shot=build_shots([a], scoreboard_events=[{'t':21, 'team':'home', 'delta':2}])[0]
        self.assertFalse(shot.made)

    def test_legacy_attempt_has_no_suggestion(self):
        a=self.attempt()
        for key in ['suggested_made','evidence','crossing_t']: a.pop(key)
        self.assertIsNone(build_shots([a])[0].suggested_made)

if __name__=='__main__': unittest.main()
