"""Geometry regressions: a bounce beside the hoop must not count as a make."""
import sys
import unittest
from pathlib import Path
ROOT=Path(__file__).resolve().parent.parent
sys.path.insert(0,str(ROOT / "src"))
from aihoop.legacy_shots.scorer import ScoreEngine
from aihoop.legacy_shots.detector import Det


def run_path(points, predicted=False, crossing_window=False, occluded_entry=False):
    engine=ScoreEngine({'score':{'enable_three_point':False,
                                 'make_window_from_crossing':crossing_window,
                                 'make_allow_occluded_entry':occluded_entry}},30)
    rim=Det('rim',1,(80,95,120,105))
    events=[]
    for i in range(2):
        events+=engine.update(i,i/30,None,rim,[])
    for i,(x,y) in enumerate(points,2):
        ball=Det('basketball',1,(x-3,y-3,x+3,y+3),predicted=predicted)
        events+=engine.update(i,i/30,ball,rim,[])
    events+=engine.finalize(len(points)+2,(len(points)+2)/30)
    return events


class CrossingTests(unittest.TestCase):
    def test_rebound_does_not_extend_entry_window(self):
        points=[(100,y) for y in (80,88,94,95,95,90,84,80)]
        points += [(100,80)]*30+[(100,y) for y in (90,98,104,112,125,140)]
        self.assertFalse(any(e['type']=='make' for e in run_path(points,crossing_window=True)))

    def test_slow_approach_then_recent_crossing_counts(self):
        events=run_path([(100,94)]*35+[(100,y) for y in (98,102,106,112,120,135)],crossing_window=True)
        self.assertEqual(sum(e['type']=='make' for e in events),1)

    def test_old_crossing_cannot_confirm_late_exit(self):
        events=run_path([(100,94)]*3+[(100,102)]*35+[(100,120)]*3,crossing_window=True)
        self.assertFalse(any(e['type']=='make' for e in events))

    def test_default_keeps_entry_window(self):
        events=run_path([(100,94)]*35+[(100,y) for y in (98,102,106,112,120,135)])
        self.assertFalse(any(e['type']=='make' for e in events))

    def test_center_drop_counts_once(self):
        events=run_path([(100,y) for y in [60,70,80,90,100,110,120,135,150,180]])
        self.assertEqual(sum(e['type']=='make' for e in events),1)
        self.assertIsNotNone(next(e for e in events if e['type']=='make')['rim_crossing_t'])

    def test_side_drop_is_not_a_make(self):
        events=run_path([(130,y) for y in [60,70,80,90,100,110,120,135,150,180]])
        self.assertFalse(any(e['type']=='make' for e in events))

    def test_rim_bounce_then_side_drop(self):
        events=run_path([(100,90),(100,95),(100,95),(100,80),(105,60),(110,50),
                         (120,60),(130,80),(132,95),(135,110),(138,130),(140,160)])
        self.assertFalse(any(e['type']=='make' for e in events))

    def test_prediction_cannot_confirm_crossing(self):
        events=run_path([(100,y) for y in [60,70,80,90,100,110,120,135,150,180]],True)
        self.assertFalse(any(e['type']=='make' for e in events))


class OccludedEntryTests(unittest.TestCase):
    """备用证据（make_allow_occluded_entry）默认必须关闭，打开后也要满足"确实落到筐下"。

    场景：球进筐瞬间被筐沿/球网/球员挡住几帧，真实观测拿不到"向下穿越筐心"，
    只在筐口内被看到一次、随后在筐下被看到一次（见 eval/diagnose_unseen_make.md）。
    """
    # 筐 Det('rim',1,(80,95,120,105)) → cx=100, cy=100, top=95, bottom=105, 筐口带 85~115
    # 三点必须知道：
    #   1) 第一帧 y=103 必须**低于筐心 100**：否则会被记成一次"向下穿越筐心"，默认规则本来就会判进，
    #      这样测的就不是备用证据了；
    #   2) 第一帧还要高于筐底 105，因为建立"出手"要求 by < rim.bottom；
    #   3) 判定用的是最近 3 次真实观测的**中位数**（_smooth_xy），只观测到一帧"筐下"不够，
    #      至少要连续两帧落在筐下，中位数才会真的到筐下。
    OCCLUDED_DROP = [(100,103),(100,140),(100,150)]

    def test_off_by_default(self):
        events=run_path(self.OCCLUDED_DROP)
        self.assertFalse(any(e['type']=='make' for e in events))

    def test_enabled_suggests_exactly_once_without_scoring(self):
        events=run_path(self.OCCLUDED_DROP,occluded_entry=True)
        self.assertEqual(len(events),1)
        self.assertEqual(events[0]['type'],'uncertain')
        self.assertIs(events[0]['suggested_made'],True)
        self.assertEqual(events[0]['points'],0)

    def test_enabled_still_requires_dropping_below_the_rim(self):
        # 只在筐口内被看到，随后横着飞走 → 不能因为开关打开就判命中
        events=run_path([(100,90),(100,105),(150,105),(200,110),(240,115)],occluded_entry=True)
        self.assertFalse(any(e['type']=='make' for e in events))

    def test_enabled_does_not_accept_ball_leaving_upward(self):
        # 在筐口内被看到后向上飞出（比如砸筐弹回）→ 不是命中
        events=run_path([(100,90),(100,105),(100,90),(100,70),(100,50)],occluded_entry=True)
        self.assertFalse(any(e['type']=='make' for e in events))


if __name__=='__main__': unittest.main(verbosity=2)
