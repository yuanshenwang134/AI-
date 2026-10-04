# -*- coding: utf-8 -*-
"""「球不是质点」的净空判据 + 筐位局部不确定度 —— 假进球（nathan 3.40s）的真实根因。

**根因不是"筐心漂移大"**（我一开始也这么以为，错了）：
  * 那次判进的球心横向偏移被算成 "0.00×rx"，是因为拿 `cross_x` 去减了**整段视频的
    筐位中值** 551.8；而 t=3.40s 那一刻的筐心其实是 533.4（镜头在动）。
  * 相对**当时**筐心，球心偏移 19px = 0.63×rx。当时阈值是 `rim_inner=0.85`——
    它把球当成**质点**，于是 0.63 被判成"穿过筐心"。
  * 球不是质点：实测该片段球半径 ≈16px、篮圈半宽 ≈29px（比值 0.55，与
    「球直径 0.24m / 篮圈内径 0.45m = 0.53」一致），球心偏移超过
    (1-0.53)·rx 时球体必然压住篮圈 —— 那是"贴筐掠过"，不是干净的穿筐。
    人工逐帧复核确认这一球"贴着筐沿/网外掉下去"，即**不中**。
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from aihoop.ball import BallCandidate                                  # noqa: E402
from aihoop.hoop import Hoop, HoopConfig, HoopTrack, detect_shots      # noqa: E402

FPS = 30.0
# nathan_freethrow.mov 实测：整段筐位中值 (551.8,153.5)，rx≈30；t=3.40s 那一刻
# 的筐心是 (533.4, …)、rx≈28.6。
HOOP = Hoop(cx=553.0, cy=154.0, rx=29.0, ry=10.0, votes=80, confidence=1.0,
            method="yolo")

# 一条真实的「升到筐上方 -> 顶点 -> 落下穿过筐面」弧线，相对筐心 cy 的像素纵坐标。
# 下标 10 是顶点（-74px），下标 15 是穿越前最后一个"在筐面上方"的点（-13 < -ry），
# 下标 16 是穿越后第一个"在筐面下方"的点（+12 > +ry）—— 穿越就发生在 15→16 之间。
_REL_Y = [210, 160, 115, 75, 40, 12, -14, -38, -58, -72, -74, -70, -60, -45,
          -28, -13, 12, 45, 82, 124, 170, 220, 275, 335, 400, 470, 545, 625]
# 横向：接近篮筐时收拢到固定偏移 off_px（出手弧线本来就朝筐收）
_FACTOR = [3.0, 2.7, 2.4, 2.1, 1.8, 1.6, 1.45, 1.35, 1.25, 1.2, 1.15, 1.1, 1.05,
           1.0, 1.0, 1.0] + [1.0] * 12


def _arc(off_px, cx=HOOP.cx):
    return [BallCandidate(t=i / FPS, x=cx + off_px * _FACTOR[i],
                          y=HOOP.cy + _REL_Y[i], score=0.9, source="test")
            for i in range(len(_REL_Y))]


def _shot(off_px, cfg=None, hoop=None):
    shots = detect_shots([_arc(off_px, (hoop or HOOP).cx)], hoop or HOOP,
                         cfg or HoopConfig())
    assert shots, "这条合成弧线本身就该被认成一次出手（否则测不到判进逻辑）"
    return shots[0]


class BallClearanceTests(unittest.TestCase):
    """净空判据：|球心偏移| ≤ (1 - 球直径/筐内径)·rx = 0.467·rx 才算干净穿筐。"""

    def test_centre_crossing_is_a_make(self):
        s = _shot(0.0)
        self.assertIs(s.made, True)
        self.assertEqual(s.evidence, "cross_measured")

    def test_real_nathan_graze_is_not_a_make(self):
        """nathan 3.40s 那一球（人工确认不中）：偏移 0.63×rx → 不许判进。"""
        s = _shot(0.63 * 29.0)
        self.assertIsNot(s.made, True, "球心偏 0.63rx、球体压住篮圈，不该判进")
        self.assertEqual(s.evidence, "cross_rim_contact")
        self.assertIsNone(s.made, "贴筐掠过：进没进看不出来，既不算进也不算不中")

    def test_real_nathan_makes_stay_makes(self):
        """同一片段里 3 个**人工确认真进**的球：偏移 0.12 / 0.20 / 0.42×rx。"""
        for k in (0.12, 0.20, 0.42):
            with self.subTest(k=k):
                s = _shot(k * 29.0)
                self.assertIs(s.made, True, f"偏移 {k}rx 是干净穿筐，必须仍然判进")

    def test_point_ball_threshold_would_have_accepted_the_graze(self):
        """对照：把门槛放回"质点球"的 0.85 → 同一段就重现那次假进球。

        这条断言的作用是证明上面的用例**确实咬得住** 3.40s 那个 bug。
        """
        cfg = HoopConfig()
        cfg.rim_inner = 0.85
        cfg.ball_diameter_m = 0.0        # 球直径当 0 = 把球当质点
        s = _shot(0.63 * 29.0, cfg)
        self.assertIs(s.made, True, "把球当质点时本应重现假进球（说明用例有效）")

    def test_way_outside_is_a_miss(self):
        s = _shot(1.30 * 29.0)
        self.assertIs(s.made, False, "球心明显落在篮圈之外 → 判定不中")


class HoopUncertaintyTests(unittest.TestCase):
    """筐位不确定度只用来"拒绝下结论"，而且必须用**局部**抖动，不能用整段漂移。"""

    def test_huge_explicit_uncertainty_abstains(self):
        cfg = HoopConfig()
        cfg.hoop_uncertainty_px = 33.0          # 33/29 = 1.14 ≥ 0.5
        s = _shot(0.0, cfg)
        self.assertIsNone(s.made, "筐位不确定度≈rx 时，正筐心穿过也不许判进")
        self.assertEqual(s.evidence, "cross_hoop_uncertain")

    def test_manual_hoop_has_no_penalty(self):
        """手标筐（常数 Hoop，没有抖动）不受影响。"""
        s = _shot(0.0)
        self.assertIs(s.made, True)
        self.assertEqual(s.evidence, "cross_measured")

    def test_smooth_pan_is_not_treated_as_uncertainty(self):
        """整段平移 40px 的**平滑摇摄**：筐心每一刻都跟得准，必须照常判进。

        这是回归：上一版直接拿 `robust_drift`（整段 10~90% 漂移）当不确定度，
        于是 nathan 的 3 个真进球连同那次假进球一起变成"未知"（召回 0/5）。
        """
        samples = [(i / 30.0, Hoop(cx=533.0 + 40.0 * i / 59.0, cy=154.0,
                                   rx=29.0, ry=10.0)) for i in range(60)]
        track = HoopTrack(samples=samples, fps=FPS, duration=59 / 30.0,
                          votes=60, frames=60, width=960)
        self.assertGreater(track.robust_drift()[0], 30.0, "整段漂移确实很大（摇摄）")
        self.assertLess(track.local_uncertainty(0.45), 1.0,
                        "平滑摇摄的局部不确定度应该接近 0")
        # 球跟着镜头一起走：x 相对**当时**的筐心偏移为 0
        ball = [BallCandidate(t=p.t, x=533.0 + 40.0 * p.t / (59 / 30.0)
                              + (p.y - HOOP.cy) * 0.0, y=p.y, score=0.9,
                              source="test") for p in _arc(0.0)]
        shots = detect_shots([ball], track, HoopConfig())
        self.assertTrue(shots, "平滑摇摄下这次出手仍该被产出")
        self.assertIs(shots[0].made, True, "平滑摇摄不该把真进球变成'未知'")

    def test_jittery_hoop_abstains(self):
        """检测抖动的筐（筐心每帧乱跳几十像素）：这一刻筐心自己就不可信 → 只报未知。"""
        # 注意：**不能**用 ±40 严格交替 —— 中值滤波会把交替抖动正好滤成 0 残差
        # （窗口内同奇偶的样本占多数）。真实抖动是杂乱的，这里用固定伪随机序列。
        offs = [0, 28, -12, 33, -25, 6, 30, -18, 24, -30, 15, -6]
        samples = [(i / 30.0, Hoop(cx=553.0 + offs[i], cy=154.0, rx=29.0, ry=10.0))
                   for i in range(len(offs))]
        track = HoopTrack(samples=samples, fps=FPS, duration=len(offs) / 30.0,
                          votes=len(offs), frames=len(offs), width=960)
        self.assertGreater(track.local_uncertainty(0.2, 1.5), 29.0 * 0.5,
                           "乱跳的轨迹，局部不确定度就该很大")
        shots = detect_shots([_arc(0.0, 553.0)], track, HoopConfig())
        self.assertTrue(shots, "事件本身应被产出（结果未知）")
        self.assertIsNone(shots[0].made, "筐心乱跳 → 不许判进")
        self.assertEqual(shots[0].evidence, "cross_hoop_uncertain")


if __name__ == "__main__":
    unittest.main()
