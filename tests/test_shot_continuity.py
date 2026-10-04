import unittest
from aihoop.legacy_shots.scorer import ScoreEngine, Attempt
from aihoop.legacy_shots.stream import LegacyShotStream, DEFAULT_CONFIG
from aihoop.legacy_shots.detector import Det

RIM = Det('rim', .9, (80, 95, 120, 105))
def ball(x,y): return Det('basketball',.9,(x-8,y-8,x+8,y+8))
def ready():
    e=ScoreEngine(DEFAULT_CONFIG,30)
    e.update(0,0,None,RIM,[]);e.update(1,.033,None,RIM,[])
    return e

class Continuity(unittest.TestCase):
    def test_single_far_observation_cannot_unlock(self):
        e=ready();e.needs_leave=True
        e.update(2,.067,ball(240,80),RIM,[])
        self.assertTrue(e.needs_leave);self.assertIsNone(e.attempt)
        e.update(3,.1,None,RIM,[])
        e.update(4,.133,ball(240,80),RIM,[])
        self.assertTrue(e.needs_leave)

    def test_rebound_below_can_unlock_without_horizontal_departure(self):
        e=ready();e.needs_leave=True
        e.update(2,.067,ball(100,150),RIM,[])
        e.update(3,.1,ball(100,150),RIM,[])
        self.assertFalse(e.needs_leave);self.assertIsNone(e.attempt)
        e.update(4,.133,ball(100,80),RIM,[])
        self.assertIsNotNone(e.attempt)

    def test_below_rim_gap_emits_unknown_and_new_attempt_is_separate(self):
        e=ready()
        e.attempt=Attempt(2,.067,(100,80),1,None,entered=True,
                          last_real_t=.1,last_real_xy=(100,120))
        events=e.update(19,.64,None,RIM,[])
        self.assertEqual(len(events),1)
        self.assertEqual(events[0]['review_reason'],'below_rim_tracking_gap')
        self.assertIsNone(events[0]['made'])
        e.update(32,1.067,ball(100,80),RIM,[])
        self.assertAlmostEqual(e.attempt.start_t,1.067)

    def test_gap_without_below_rim_evidence_does_not_split(self):
        e=ready()
        e.attempt=Attempt(2,.067,(100,80),1,None,entered=True,
                          last_real_t=.1,last_real_xy=(100,90))
        self.assertEqual(e.update(19,.64,None,RIM,[]),[])
        self.assertIsNotNone(e.attempt)

    def test_old_median_cannot_create_new_shot_from_far_ball(self):
        e=ready();e.recent_real.extend([(100,70)]*3);e.last_observed_t=.1
        e.update(30,1.,ball(100,300),RIM,[])
        self.assertIsNone(e.attempt)
        self.assertEqual(list(e.recent_real),[(100,300)])

    def test_stale_descent_cannot_confirm_make(self):
        e=ready()
        e.attempt=Attempt(2,.067,(100,80),1,None,entered=True,
            entered_t=.1,entered_cy=99,last_real_t=.1,last_real_xy=(100,99),
            last_real_cy=99,crossing_t=.1,real_obs=3)
        events=e.update(19,.64,ball(100,120),RIM,[])
        self.assertFalse(any(ev['type']=='make' for ev in events))

    def test_long_gap_does_not_allow_550_pixel_jump(self):
        s=LegacyShotStream(30,1920)
        s.update(0,0,[Det('basketball',.9,(980,210,1010,240))])
        fake=Det('basketball',.9,(708,669,738,699))
        s.update(10,10/30,[fake])
        self.assertIsNone(s.frames[-1]['ball'])
        self.assertEqual(len(s.frames[-1]['ball_candidates']),1)
        self.assertEqual((s.vx,s.vy),(0,0))

    def test_short_gap_valid_motion_is_retained(self):
        s=LegacyShotStream(30,1920)
        s.update(0,0,[Det('basketball',.9,(980,210,1010,240))])
        s.update(1,1/30,[Det('basketball',.9,(985,220,1015,250))])
        self.assertFalse(s.frames[-1]['ball']['predicted'])

if __name__=='__main__': unittest.main()
