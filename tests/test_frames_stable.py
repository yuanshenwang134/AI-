"""自动挑帧：必须挑**相邻**两帧，不能挑"整段镜头的首尾"。

为什么单独钉这一条（实测两次踩坑）：
  ① 用户手动取两帧（t=4.75s / t=58.5s）→ 跨镜头，合并解必然矛盾；
  ② 第一版自动挑帧取"最长一段镜头的首尾"（t=1.24s / t=24.89s，相隔 23.6s）
     → 那 23.6 秒里镜头又切了好几次，**照样不是同一机位**。
     实测相邻 3 秒的内点率序列：99% / 29% / 53% / 55% / 29% / 65% / 10% / 35%
     —— 中间断了三次，说明"整段"这个假设根本不成立。

实测"隔多久还算同一镜头"（内点率 ≥35% 判据，沿时间轴多起点取最差值）：

    | 间隔 | 1s  | 2s  | 4s  | 6s  | 8s  |
    |------|-----|-----|-----|-----|-----|
    | 最差 | 52% | 51% | 19% | 8%  | 14% |

所以取相邻 1~2 秒。这个测试只验**不变量**（不依赖具体视频内容）：
返回的两个时刻必须真的相邻（间隔 ≤ 阈值），且两者之间的判定必须是"同一镜头"。
"""
from __future__ import annotations

import asyncio
import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))

from aihoop import api            # noqa: E402
from aihoop.frame_motion import estimate_motion  # noqa: E402

VIDEO = os.path.join(ROOT, "data", "uploads", "basketball_match_3min.mp4")
MAX_GAP = 3.0          # 相隔超过这个秒数就不该被当成"同一镜头"


def _need_video():
    if not os.path.exists(VIDEO):
        pytest.skip("示例视频不在（data/uploads/basketball_match_3min.mp4）")


def test_picked_pair_is_adjacent():
    """挑出来的两帧必须相邻（不是"整段的首尾"）。"""
    _need_video()
    r = asyncio.run(api.get_frames_stable(VIDEO, 8, 1.5))
    if not r.get("ok"):
        pytest.skip("这段素材没通过（%s）" % str(r.get("note"))[:60])
    t1, t2 = r["t1"], r["t2"]
    assert t1 is not None and t2 is not None
    assert 0 < (t2 - t1) <= MAX_GAP, \
        "两帧相隔 %.2fs，太远（应 ≤%.1fs）—— 相隔久了两帧很容易跨镜头" % (t2 - t1, MAX_GAP)


def test_picked_pair_really_is_the_same_shot():
    """再用 estimate_motion 独立复核一遍：这两帧必须判为同一镜头。"""
    _need_video()
    r = asyncio.run(api.get_frames_stable(VIDEO, 8, 1.5))
    if not r.get("ok"):
        pytest.skip("这段素材没通过")
    m = estimate_motion(VIDEO, r["t1"], r["t2"])
    assert m["ok"], "挑出来的两帧被判为不同镜头：%s" % m["note"]


def test_rejects_when_gap_too_large_to_be_a_shot():
    """间隔给得很大时，**不应该**还硬说"同一镜头"（宁可如实说没通过）。"""
    _need_video()
    r = asyncio.run(api.get_frames_stable(VIDEO, 6, 8.0))
    if r.get("ok"):
        # 若它仍返回 ok，那两帧之间必须真的通过判定（说明这段素材确实连续跟拍）
        m = estimate_motion(VIDEO, r["t1"], r["t2"])
        assert m["ok"], ("gap=8s 时返回了 ok，但 t=%s→%s 并不在同一镜头"
                         % (r["t1"], r["t2"]))
