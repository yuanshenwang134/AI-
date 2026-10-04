# -*- coding: utf-8 -*-
"""nybo 两个假进球的**合成回归**：球贴着篮圈外沿斜掠而下、从未进内圈 → 不得判进。

为什么用合成轨迹而不是原片：原片 `<旧项目>/data/bili_nybo.mp4` 已不在本机
（全盘搜不到；旧项目全部 git 历史里也没有下载记录），无法用真实素材固化回归。
而当时那两个假进球的**几何描述是明确的**（`hoopsight.py` 里留着）：
    「球贴着篮圈右外缘斜掠而下，球心横向偏移一直在 0.4~1.1×rx，从没真正进过圈」
    —— 修复办法不是放宽/收紧"穿筐点"（`cross_inner`），而是**再要求球真的进过内圈**
    （`cup_inner = 0.62×rx` 内至少要有证据帧）。
所以这里把该失败模式参数化成几何用例：**只要没有帧真正落在内圈里，就不许判进**；
并保留一条"把 cup_inner 放回 1.1 就会重现假进球"的对照断言，
确保这个测试确实咬得住当年那个 bug，而不是空转。

原片若日后找回，用真实素材再复核一遍即可（见 eval/ground_truth/nybo回归说明.md）。
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from aihoop.hoop import Hoop                                      # noqa: E402
from aihoop.hoopsight import SightConfig, _shots_from_blob_series  # noqa: E402

HOOP = Hoop(cx=100.0, cy=60.0, rx=40.0, ry=18.0, votes=10,
            confidence=0.9, method="test")
# 内圈门槛：cup_inner 0.62×rx = 24.8px；穿筐点门槛 cross_inner 0.90×rx = 36px
CUP_PX = 0.62 * 40.0
CROSS_PX = 0.90 * 40.0
FPS = 24.0


def _blob(t, cx, cy, area=400.0):
    """与 tests/test_hoopsight.py 同一构造：块就是 (t, cx, cy, area) 四元组。"""
    return (round(t, 3), cx, cy, area)


def _chain(off_px, fps=FPS):
    """先升到筐上方、再朝下落过筐面；**穿筐点横向偏移固定为 off_px**。"""
    rel_y = [-45.0, -25.0, -12.0, 4.0, 22.0, 45.0, 70.0]
    xs = [HOOP.cx + off_px * k for k in (1.7, 1.4, 1.1, 1.0, 1.0, 1.0, 1.0)]
    return [_blob(i / fps, x, HOOP.cy + dy) for i, (x, dy) in enumerate(zip(xs, rel_y))]


def _made(off_px, cfg=None, fps=FPS):
    shots = _shots_from_blob_series(_chain(off_px, fps), HOOP, cfg or SightConfig(),
                                    fps=fps)
    return None if not shots else shots[0].made


class NyboRimGrazeTests(unittest.TestCase):
    def test_centre_crossing_is_a_make(self):
        """对照组：球真的从内圈穿过（0.25×rx）→ 判进。"""
        self.assertIs(_made(0.25 * 40.0), True)

    def test_graze_outside_cup_is_never_a_make(self):
        """nybo 失败模式：穿筐点在内圈之外（0.70/0.85×rx）→ 不许判进。"""
        for k in (0.70, 0.85):
            with self.subTest(k=k):
                self.assertIsNot(_made(k * 40.0), True,
                                 f"球心横向 {k}×rx、从未进内圈，不该判进")

    def test_crossing_at_rim_edge_is_not_a_make(self):
        """0.95×rx：连"穿筐点"都不算（>cross_inner）→ 更不该判进。"""
        self.assertIsNot(_made(0.95 * 40.0), True)

    def test_old_loose_cup_setting_reproduces_the_false_make(self):
        """对照：把 cup_inner 放回 1.1（当年那档）→ 同一段就变成假进球。

        这条断言的作用是证明上面的用例**确实能咬住** nybo 那个 bug；
        如果哪天有人把 cup_inner 放宽回去，上面那条 test_graze_* 会失败。
        """
        cfg = SightConfig()
        cfg.cup_inner = 1.1
        self.assertIs(_made(0.70 * 40.0, cfg), True,
                      "放宽 cup_inner 后本应重现假进球（说明用例有效）")

    def test_thresholds_stay_in_the_measured_safe_range(self):
        """把当时的实测结论写死：内圈门槛必须比穿筐点门槛**明显更严**。"""
        cfg = SightConfig()
        self.assertLess(cfg.cup_inner, cfg.cross_inner,
                        "cup_inner 必须比 cross_inner 更严，否则贴筐掠过的球又会变成进球")
        self.assertLessEqual(cfg.cross_inner, 0.90)


if __name__ == "__main__":
    unittest.main()
