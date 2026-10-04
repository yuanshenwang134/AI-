# -*- coding: utf-8 -*-
"""米制下落门槛 + 筐口空洞趋势外推（`detect_shots` 的两处改动）。

用**合成轨迹**把两条判据单独隔离出来测，不依赖视频与检测器：
  1. `min_cross_drop_m`：薄筐（ry 很小）时，原来的"下落 ≥1 倍 ry"实际只要求几个像素，
     米制下限会把这条门槛按物理尺度抬起来 —— 用一对只差 7.9px 的穿越点验证。
  2. `allow_extrapolated_crossing`：球在筐口被网挡住（检测空洞）时，
     默认仍不判进；打开后改用 bracket 内所有点的拟合交点，且只对更靠筐心的落点放行。
三条护栏（圈外、超长空洞）必须仍然拦住。
"""
import unittest

from aihoop.ball import BallCandidate
from aihoop.hoop import Hoop, HoopConfig, detect_shots

FPS = 30.0


def arc(rel_y, xs, t0=0.0):
    """按给定的「相对篮筐中心的 y」与 x 序列造轨迹。"""
    return [BallCandidate(t=t0 + i / FPS, x=float(x), y=None, score=0.9,
                          source="test") if False else
            BallCandidate(t=t0 + i / FPS, x=float(x), y=float(y), score=0.9,
                          source="test")
            for i, (y, x) in enumerate(zip(rel_y, xs))]


class MetricDropFloorTests(unittest.TestCase):
    """薄筐：手标篮筐 ry 只有 3.9px 时，"1 倍 ry"其实只有 3.9 像素。"""

    def setUp(self):
        self.hoop = Hoop(cx=500, cy=300, rx=17.9, ry=3.9)   # 与 fixedcam 手标薄筐同量级
        # 上升→顶点→下落；最后两个点刚好卡在筐带两侧，drop = 7.9px
        rel = [-60, -100, -120, -90, -50, -20, -3.95, 3.95, 30, 70]
        xs = [440, 455, 470, 482, 490, 496, 499, 501, 505, 512]
        self.track = [BallCandidate(t=i / FPS, x=xs[i], y=300 + rel[i],
                                    score=0.9, source="test")
                      for i in range(len(rel))]

    def test_metric_floor_rejects_tiny_drop(self):
        cfg = HoopConfig()
        cfg.min_cross_drop_m = 0.10                  # 17.9/0.225*0.10 = 7.96px
        shot = detect_shots([self.track], self.hoop, cfg)[0]
        self.assertIsNone(shot.made,
                          "下落 7.9px 小于米制下限 7.96px，不该判进")

    def test_without_metric_floor_same_track_is_make(self):
        cfg = HoopConfig()
        cfg.min_cross_drop_m = 0.0                   # 退回"1 倍 ry"(3.9px) 的老判据
        shot = detect_shots([self.track], self.hoop, cfg)[0]
        self.assertIs(shot.made, True,
                      "同一段轨迹在老判据下确实会判进 —— 说明差别来自米制门槛本身")


class ExtrapolatedCrossingTests(unittest.TestCase):
    """筐口空洞：球在网后漏检 0.43s（nathan 实测那一球的形状）。"""

    def setUp(self):
        self.hoop = Hoop(cx=553, cy=154, rx=31, ry=10)
        # 先正常上升→顶点（让 saw_rise 成立，事件才会被产出），再下落；
        # 前段最后一个点 -20 在"明确上方"带内、-8 已不在，
        # 空洞 0.43s，之后 3 个点在筐下方；横向收敛到筐心附近
        pre = [(-30, 400), (-60, 430), (-90, 470), (-110, 500), (-100, 515),
               (-70, 525), (-40, 530), (-20, 532), (-8, 545)]
        post = [(20, 552), (45, 554), (70, 556)]
        pts = []
        for i, (ry_, x) in enumerate(pre):
            pts.append(BallCandidate(t=i / FPS, x=float(x), y=154 + ry_,
                                     score=0.9, source="test"))
        for i, (ry_, x) in enumerate(post):
            pts.append(BallCandidate(t=0.1 + 0.467 + i / FPS, x=float(x),
                                     y=154 + ry_, score=0.9, source="test"))
        self.track = pts

    @staticmethod
    def made_of(shots):
        return shots[0].made if shots else None

    def test_hole_is_not_interpolated_by_default(self):
        shot = self.made_of(detect_shots([self.track], self.hoop, HoopConfig()))
        self.assertIsNone(shot, "默认策略：筐口空洞不判进")

    def test_flag_recovers_central_crossing(self):
        cfg = HoopConfig()
        cfg.allow_extrapolated_crossing = True
        shots = detect_shots([self.track], self.hoop, cfg)
        self.assertTrue(shots, "事件本身应该被产出")
        self.assertIs(shots[0].made, True, "打开开关后，靠趋势拟合应能判进")
        self.assertEqual(shots[0].evidence, "cross_extrapolated")
        self.assertLessEqual(shots[0].confidence, 0.7, "外推档要降置信度")

    def test_flag_does_not_accept_crossing_outside_rim(self):
        cfg = HoopConfig()
        cfg.allow_extrapolated_crossing = True
        off = 260.0                                  # 整体左移，交点会落在圈外
        moved = [BallCandidate(t=p.t, x=p.x - off, y=p.y, score=p.score,
                               source=p.source) for p in self.track]
        self.assertIsNot(self.made_of(detect_shots([moved], self.hoop, cfg)), True,
                         "圈外的外推交点不许判进")

    def test_flag_does_not_bridge_long_hole(self):
        """bracket 总时长超过 extrap_max_bracket_s（但仍落在同一段轨迹里）。"""
        cfg = HoopConfig()
        cfg.allow_extrapolated_crossing = True
        rows = [(0.00, -30, 400), (0.033, -60, 430), (0.067, -90, 470),
                (0.100, -110, 500), (0.200, -20, 532), (0.500, -5, 545),
                (0.850, 20, 552), (0.900, 45, 554), (0.950, 70, 556)]
        track = [BallCandidate(t=t, x=float(x), y=154.0 + ry_, score=0.9,
                               source="test") for (t, ry_, x) in rows]
        self.assertIsNot(self.made_of(detect_shots([track], self.hoop, cfg)), True,
                         "空洞的 bracket 超过 0.6s 不许外推")


class PixelPerMetreTests(unittest.TestCase):
    def test_prefers_board_height(self):
        from aihoop.hoop import _px_per_m
        h = Hoop(cx=0, cy=0, rx=20, ry=5, board=[-30, -60, 60, 105])   # 板高 105px=1.05m
        self.assertAlmostEqual(_px_per_m(h), 100.0, places=6)

    def test_falls_back_to_rim_radius(self):
        from aihoop.hoop import _px_per_m
        h = Hoop(cx=0, cy=0, rx=22.5, ry=6)
        self.assertAlmostEqual(_px_per_m(h), 100.0, places=6)


if __name__ == "__main__":
    unittest.main()
