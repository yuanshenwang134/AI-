"""多镜头标定：**两台机位各解一份 H**，再在结果层合并。

为什么需要这个模块（用户原话）：
    "一个镜头怎么可能看到全场啊，就是两个镜头标点，按照标出来的点算战术图
     和投篮热区啊。"

这是对的：一台机位拍不到全场，所以真实工作流是两个机位各拍半场。
而两个机位**没有共同坐标系** —— 把两边的点混在一起解**一个**单应矩阵在数学上
不成立（用户反复看到的"解算失败 / 点互相矛盾"就是这么来的）。
正确做法：每个镜头各解一份 H，各自把自己覆盖的部分投到球场坐标，再在结果层合并。

本文件钉住这条链路的基础设施：分段、按时刻取段、序列化、分镜解算、
"两个标点之间有没有切镜"的定位。
"""
from __future__ import annotations

import os
import pathlib
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))

from aihoop.court import find_homography            # noqa: E402
from aihoop.multical import (MultiCal, Segment,     # noqa: E402
                             _solve_one, plan_segments)

VIDEO = os.path.join(ROOT, "data", "uploads", "basketball_match_3min.mp4")
W, H = 960, 544
# 一份"像样"的机位：球场四角
QUAD_PX = [[-80.0, 430.0], [1040.0, 430.0], [900.0, 90.0], [60.0, 90.0]]
QUAD_M = [[-7.5, -14.0], [7.5, -14.0], [7.5, 14.0], [-7.5, 14.0]]
H_TRUE = find_homography([list(p) for p in QUAD_PX], [list(p) for p in QUAD_M])


def _seg(t0, t1, ratio=1.9, n=7, Hm=None):
    return Segment(t_start=t0, t_end=t1, src_px=[list(p) for p in QUAD_PX],
                   dst_m=[list(p) for p in QUAD_M], H=list(Hm if Hm is not None
                                                           else H_TRUE),
                   frame="full", names=["a"] * 4, rmse_m=1.1, ratio=ratio,
                   n_points=n)


def _proj(Hm, x, y):
    import numpy as np
    M = np.asarray(Hm, dtype=np.float64)
    q = M @ np.array([float(x), float(y), 1.0])
    return float(q[0] / q[2]), float(q[1] / q[2])


def test_segment_at_picks_the_right_shot():
    mc = MultiCal([_seg(0, 60), _seg(60, 180)])
    assert mc.segment_at(10).t_end == 60
    assert mc.segment_at(60).t_end == 180      # 边界归后一段（切镜那一刻之后是新镜头）
    assert mc.segment_at(100).t_start == 60


def test_segment_at_falls_back_to_nearest_outside_range():
    """标点只覆盖了部分镜头，视频首尾之外要取最近的一段，不能返回 None。"""
    mc = MultiCal([_seg(60, 180)])
    assert mc.segment_at(0) is not None
    assert mc.segment_at(9999) is not None
    assert mc.segment_at(0).t_start == 60


def test_primary_is_the_richest_segment():
    mc = MultiCal([_seg(0, 60, n=5), _seg(60, 180, n=9)])
    assert mc.primary.n_points == 9


def test_roundtrip_preserves_segments():
    mc = MultiCal([_seg(0, 60), _seg(60, 180)], for_video="v.mp4",
                  frame_size=[W, H], note="两段")
    d = mc.to_dict()
    assert d.get("multi_shot") is True
    assert len(d.get("segments") or []) == 2
    # 顶层仍然写一份"主标定"的字段，老代码/老界面读得懂
    assert d.get("H") and d.get("src_px") and d.get("dst_m")
    mc2 = MultiCal.from_dict(d)
    assert mc2 is not None and len(mc2.segments) == 2
    assert mc2.segment_at(100).t_end == 180
    assert mc2.for_video == "v.mp4"


def test_from_dict_without_segments_returns_none():
    """单镜头的老标定文件没有 segments —— 调用方据此走老路径。"""
    assert MultiCal.from_dict({"H": H_TRUE}) is None


def test_plan_segments_keeps_only_marked_shots():
    """只有标过点的镜头才留 —— 没标的镜头没有标定，留着没意义。"""
    got = plan_segments(180.0, [60.0], [10.0, 100.0])
    assert got == [(0.0, 60.0, [10.0]), (60.0, 180.0, [100.0])]
    only_first = plan_segments(180.0, [60.0], [10.0])
    assert len(only_first) == 1 and only_first[0][0] == 0.0


def test_solve_one_produces_exact_fit_on_consistent_points():
    """同一镜头内、几何一致的点，解出来应当几乎零误差。"""
    import numpy as np
    Hinv = np.linalg.inv(np.asarray(H_TRUE, dtype=np.float64))
    names = ["lane_far_left", "lane_far_right", "lane_near_right", "lane_near_left"]
    court = [[-2.45, 8.2], [2.45, 8.2], [2.45, -8.2], [-2.45, -8.2]]
    px = [list(_proj(Hinv, x, y)) for (x, y) in court]
    seg, why = _solve_one([{"t": 10.0, "px": px, "dst": court, "names": names,
                            "bgr": None}], W, H)
    assert seg is not None, why
    assert seg.n_points == 4
    assert seg.rmse_m < 0.01, "一致的 4 点应当精确解出：%.3f m" % seg.rmse_m
    # ⚠️ 用 `cal.to_court` 比对时要注意：`frame="full"` 的标定会走
    # fold_to_analysis（折半分析坐标），所以全场 y=8.2 折半后是 5.8。
    # 这不是 bug —— 是我第一版测试的期望写错了（拿全场坐标去比折半结果）。
    from aihoop.court import fold_to_analysis
    cal = seg.calibration()
    for p, (x, y) in zip(px, court):
        u, v = cal.to_court(p[0], p[1])
        ex, ey = fold_to_analysis(x, y, "full")
        assert abs(u - ex) < 0.02 and abs(v - ey) < 0.02, \
            "投回 (%.2f,%.2f) 期望 (%.2f,%.2f)" % (u, v, ex, ey)
    # 再用 H 直接验证"原样投回"（不折半）
    from aihoop.court import apply_homography
    for p, (x, y) in zip(px, court):
        u, v = apply_homography(cal.H, p[0], p[1])
        assert abs(u - x) < 0.02 and abs(v - y) < 0.02


def test_solve_one_refuses_too_few_points():
    seg, why = _solve_one([{"t": 1.0, "px": [[1, 2], [3, 4]], "dst": [[0, 0], [1, 1]],
                            "names": ["a", "b"], "bgr": None}], W, H)
    assert seg is None and "2 个点" in why


@pytest.mark.skipif(not os.path.exists(VIDEO), reason="示例视频不在")
def test_split_times_by_shot_separates_known_different_shots():
    """t=5s 与 t=58.5s 是两个镜头（实测过）—— 必须被分成两组。"""
    from aihoop.multical import split_times_by_shot
    groups, cuts = split_times_by_shot(VIDEO, [5.0, 58.5])
    assert len(groups) == 2, "这两个时刻属于不同镜头，应当分成两组：%s" % groups
    assert cuts and 5.0 < cuts[0] < 58.5, "切镜时刻应当落在两者之间：%s" % cuts


@pytest.mark.skipif(not os.path.exists(VIDEO), reason="示例视频不在")
def test_split_times_by_shot_keeps_close_times_together():
    """相隔 1.5 秒的两帧是同一镜头 —— 必须留在同一组（否则会被误切）。"""
    from aihoop.multical import split_times_by_shot
    groups, _cuts = split_times_by_shot(VIDEO, [15.2, 16.7])
    assert len(groups) == 1, "相邻 1.5s 不该被切成两段：%s" % groups


# --------------------------------------------------------------- 写盘要备份
def _workdir(name: str):
    """测试用目录 —— 必须放在**工作区内**。

    为什么不用 tempfile：本机沙箱禁止写 %TEMP%，用它只会得到
    PermissionError，测试就变成在测环境而不是测代码（实测踩到）。
    """
    import shutil
    d = pathlib.Path(ROOT) / "_tmp" / ("pytest_" + name)
    shutil.rmtree(d, ignore_errors=True)
    d.mkdir(parents=True, exist_ok=True)
    return d


def test_write_calibration_keeps_two_levels_of_backup():
    """标定文件**不被 git 跟踪**，覆盖就找不回来 —— 必须自动备份。

    背景：我测试时直接用真实的标定路径写了一份合成的假标定，
    把用户手工点出来的标定覆盖掉了，git 里没有、out/ 里也没有副本，
    **彻底找不回来**。这条测试保证以后至少能退回两步。
    """
    import json
    import shutil
    from aihoop import api

    d = _workdir("calbak")
    try:
        p = d / "calibration_x.json"
        api._write_calibration(p, {"v": 1})
        assert p.exists() and not (d / "calibration_x.json.bak").exists()
        api._write_calibration(p, {"v": 2})
        assert json.loads(p.read_text(encoding="utf-8"))["v"] == 2
        bak = d / "calibration_x.json.bak"
        assert bak.exists() and json.loads(bak.read_text(encoding="utf-8"))["v"] == 1
        api._write_calibration(p, {"v": 3})
        assert json.loads((d / "calibration_x.json.bak").read_text(encoding="utf-8"))["v"] == 2
        assert json.loads((d / "calibration_x.json.bak1").read_text(encoding="utf-8"))["v"] == 1
        # 原子写：不能留下半截临时文件
        assert not list(d.glob("*.tmp"))
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_write_calibration_is_atomic_and_survives_bad_data():
    """写盘走"临时文件 + replace"：中途不会把原标定截断成半截 JSON。"""
    import json
    import shutil
    from aihoop import api

    d = _workdir("calatomic")
    try:
        p = d / "c.json"
        api._write_calibration(p, {"good": True})
        before = p.read_text(encoding="utf-8")
        from dataclasses import dataclass

        @dataclass
        class Weird:
            x: object

        try:
            api._write_calibration(p, {"bad": Weird(object())})
        except Exception:                       # noqa: BLE001
            pass
        # 无论上面成不成功，原文件都必须是**完整可解析**的
        assert json.loads(p.read_text(encoding="utf-8")) == json.loads(before)
    finally:
        shutil.rmtree(d, ignore_errors=True)


# ------------------------------------------------------- 分析端按帧取标定
def test_cal_at_picks_per_shot_during_analysis():
    """分析时按"这一帧属于哪个镜头"取标定；单机位时行为不变。"""
    from aihoop.court import Calibration
    from aihoop.sources import VideoSource

    class Fake(VideoSource):
        def __init__(self, cal, multi):        # 绕过真正的 __init__
            self.cal = cal
            self.multi_cal = multi

    H2 = find_homography([[0.0, 0.0], [10.0, 0.0], [10.0, 10.0], [0.0, 10.0]],
                         [[0.0, 0.0], [1.0, 0.0], [1.0, 1.0], [0.0, 1.0]])
    cal_a = Calibration(name="a", method="m", H=list(H_TRUE))
    cal_b = Calibration(name="b", method="m", H=list(H2))

    # 单机位：任何时刻都用 self.cal
    single = Fake(cal_a, None)
    assert single._cal_at(0).name == "a"
    assert single._cal_at(9999).name == "a"

    # 多机位：按时间段取
    # ⚠️ 两段必须给**不同的 H** —— 我第一版两段都用了 H_TRUE，
    # 于是"投影结果相同"是必然的，测出来的是我自己造的错误期望。
    mc = MultiCal([_seg(0, 60), _seg(60, 300, Hm=H2)])
    multi = Fake(cal_a, mc)
    assert multi._cal_at(10) is not None
    assert multi._cal_at(100) is not None
    # 两份 H 不同 → 同一像素投出来的球场坐标必须不同（证明确实换了标定）
    u1, v1 = multi._cal_at(10).to_court(500.0, 300.0)
    u2, v2 = multi._cal_at(100).to_court(500.0, 300.0)
    assert (abs(u1 - u2) > 1e-6 or abs(v1 - v2) > 1e-6), \
        "两段用了不同的 H，投影结果不该完全相同"
    # 找不着对应段时退回 self.cal，不能返回 None
    assert Fake(cal_a, MultiCal([]))._cal_at(5).name == "a"
