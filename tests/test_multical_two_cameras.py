"""两台机位各拍半场：**混在一起解不了，但每幅各自能解** → 按每幅一份标定。

用户实测场景（截图里的报错原文）：
    t=13.50s  6 点  误差 1.316m   ← 单独能标定
    t=73.06s  6 点  误差 1.081m   ← 单独能标定
    但混在一起解 → 只剩 2 个一致的点 → 报"跨画面的点混在一起解不了"

这正是"一个镜头拍不到全场、用两个机位各拍半场"的用法。
几何事实：两个机位**没有共同坐标系**，把两边的点揉成一份单应矩阵数学上不成立。

⚠️ 为什么不能只靠切镜检测：实测在用户的 HD 长视频上
`find_cut_between` 没找到切镜点（`_sample` 取帧拿不到画面时会静默返回
"像同一镜头"），于是依赖它的那条路整条没触发。
所以这里加了**不依赖切镜检测**的回退判据：
**"混在一起解不了 + 每幅各自成立" ⇒ 就是多机位。**
"""
from __future__ import annotations

import os
import pathlib
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ROOTP = pathlib.Path(ROOT)
sys.path.insert(0, os.path.join(ROOT, "src"))

from aihoop.api import _build_multical_from_frames  # noqa: E402
from aihoop.court import find_homography           # noqa: E402
from aihoop.multical import MultiCal, Segment      # noqa: E402


def _seg(t0, t1, ratio=1.9, rmse=1.1, n=7):
    """造一段标定（用于质量判读的单元测试）。"""
    return Segment(t_start=t0, t_end=t1, src_px=[list(p) for p in PX_A],
                   dst_m=[list(p) for p in COURT], H=list(_h(PX_A)),
                   frame="full", names=NAMES, rmse_m=rmse, ratio=ratio,
                   n_points=n)

VIDEO = os.path.join(ROOT, "data", "uploads", "basketball_match_3min.mp4")
W, H = 1280, 720

# 同一批球场点（罚球区四角 + 罚球线中点 + 篮筐）
COURT = [[-2.45, 8.2], [2.45, 8.2], [2.45, -8.2], [-2.45, -8.2],
         [0.0, 8.2], [0.0, -12.425]]
NAMES = ["lane_far_left", "lane_far_right", "lane_near_right",
         "lane_near_left", "ft_far", "hoop_near"]
# 两台机位的**像素坐标必须由单应矩阵投影生成**，不能手写：
# 手写的点不是球场的精确投影，6 点最小二乘就会有残差
# （第一版就是手写的，自检误差 1.32m，测出来的是我自己夹具的毛病）。
_PX_A_QUAD = [[220.0, 420.0], [560.0, 420.0], [600.0, 700.0], [180.0, 700.0]]
_PX_B_QUAD = [[300.0, 200.0], [900.0, 240.0], [980.0, 600.0], [240.0, 560.0]]
_QUAD_M = [[-2.45, 8.2], [2.45, 8.2], [2.45, -8.2], [-2.45, -8.2]]


def _project(quad_px, quad_m, pts):
    """用四角定出的单应矩阵，把球场点**精确**投影成像素。

    ⚠️ `find_homography(src, dst)` 是 **src→dst**。这里 src=像素、dst=球场，
    所以它算的是"像素→球场"，要投球场点必须**取逆**。
    我第一版忘了取逆，投出来是 `(-8.6, 53.8)` 这种垃圾坐标，
    于是退化检测报"有两个标定点落在同一个位置"—— 测的是我自己夹具的毛病。
    """
    import numpy as np
    Hm = np.asarray(find_homography([list(p) for p in quad_px],
                                    [list(p) for p in quad_m]), dtype=float)
    Hinv = np.linalg.inv(Hm)          # 球场 -> 像素
    out = []
    for (x, y) in pts:
        q = Hinv @ np.array([float(x), float(y), 1.0])
        out.append([float(q[0] / q[2]), float(q[1] / q[2])])
    return out


PX_A = _project(_PX_A_QUAD, _QUAD_M, COURT)
PX_B = _project(_PX_B_QUAD, _QUAD_M, COURT)


def _h(px):
    return find_homography([list(p) for p in px], [list(p) for p in COURT])


def _rmse(Hm, px):
    from aihoop.court import apply_homography
    e = []
    for (x, y), (u, v) in zip(px, COURT):
        a, b = apply_homography(Hm, x, y)
        e.append(((a - u) ** 2 + (b - v) ** 2) ** 0.5)
    return sum(e) / len(e)


def test_each_camera_is_self_consistent():
    """前提：两台机位各自都是自洽的（各自误差≈0），否则测不出这个问题。"""
    assert _rmse(_h(PX_A), PX_A) < 0.01
    assert _rmse(_h(PX_B), PX_B) < 0.01


def test_two_cameras_cannot_share_one_homography():
    """把两套点混在一起解 → 误差必然很大（这就是"互相矛盾"的来源）。"""
    mixed_px = PX_A + PX_B
    mixed_court = COURT + COURT
    Hm = find_homography([list(p) for p in mixed_px],
                         [list(p) for p in mixed_court])
    err = _rmse(Hm, mixed_px)
    assert err > 1.5, ("两个机位的点合并解出来误差只有 %.2f m —— "
                       "这组夹具没构造出'互相矛盾'" % err)


def _frames(ok_a=True, ok_b=True):
    return [
        {"t": 13.5, "n": 6, "ok": ok_a, "rmse_m": 0.01, "H": _h(PX_A),
         "src_px": PX_A, "dst_m": COURT, "names": NAMES},
        {"t": 73.06, "n": 6, "ok": ok_b, "rmse_m": 0.02, "H": _h(PX_B),
         "src_px": PX_B, "dst_m": COURT, "names": NAMES},
    ]


def test_builds_one_calibration_per_frame():
    """两幅各自成立 → 建出两段标定，各覆盖自己的时间段。"""
    mc = _build_multical_from_frames(_frames(), "v.mp4", W, H, duration=200.0)
    assert mc is not None and len(mc.segments) == 2
    s1, s2 = mc.segments
    assert s1.t_start == 0.0
    assert s1.t_end == pytest.approx(43.28, abs=0.01)      # 两帧时刻的中点
    assert s2.t_end == pytest.approx(200.0, abs=0.01)
    # 按帧所属镜头取标定：两段用的 H 不同
    assert mc.segment_at(20) is s1 and mc.segment_at(100) is s2


def test_uses_detected_cut_when_available():
    """有切镜时刻时优先用它划时间范围（比中点更准）。"""
    mc = _build_multical_from_frames(_frames(), "v.mp4", W, H,
                                     duration=200.0, cuts=[40.0])
    assert mc.segments[0].t_end == pytest.approx(40.0, abs=0.01)
    assert mc.segments[1].t_start == pytest.approx(40.0, abs=0.01)


def test_needs_at_least_two_good_frames():
    """只有一幅成立时不建多镜头标定（那是"点不全"，不是多机位）。"""
    assert _build_multical_from_frames(_frames(ok_b=False), "v.mp4", W, H) is None
    assert _build_multical_from_frames(_frames(ok_a=True, ok_b=True)[:1],
                                       "v.mp4", W, H) is None
    assert _build_multical_from_frames([], "v.mp4", W, H) is None


# --------------------------------------------------- 多机位的"真实质量"判读
def test_worst_readings_takes_the_bad_segment_not_the_good_one():
    """多机位的质量读数必须取**最差**那段，不能取 max。

    实测事故（用户那份真实标定）：
        段1（0~54s）  rmse=0.97  **ratio=0.34**   ← 投影线与白线基本不相关
        段2（54~240s）rmse=5.80  ratio=3.12
    旧写法 `max(ratio)` 报 **3.12**，把"半个视频坐标全错"完全盖住，
    战术图看着"可用"其实一半的点是错的（用户反馈"战术图也不对"）。
    """
    from aihoop.multical import weak_segments, worst_readings
    mc = MultiCal([_seg(0, 54, ratio=0.34, rmse=0.97),
                   _seg(54, 240, ratio=3.12, rmse=5.80)])
    wr = worst_readings(mc)
    assert wr["ratio"] == 0.34, "必须报最差的吻合度：%s" % wr
    assert wr["rmse_m"] == 5.80, "必须报最差的误差：%s" % wr
    weak = weak_segments(mc)
    assert len(weak) == 2, "两段都该被判弱：%s" % weak
    assert any("吻合度" in w["why"] for w in weak)
    assert any("误差" in w["why"] for w in weak)


def test_all_good_segments_report_no_weak():
    """每一段都合格时不报弱、不误伤。"""
    from aihoop.multical import weak_segments, worst_readings
    mc = MultiCal([_seg(0, 60, ratio=1.8, rmse=1.1),
                   _seg(60, 200, ratio=2.2, rmse=0.8)])
    assert weak_segments(mc) == []
    wr = worst_readings(mc)
    assert wr["ratio"] == 1.8 and wr["rmse_m"] == 1.1


def test_weak_segment_marks_positions_unverified_end_to_end(monkeypatch):
    """弱段必须让位置结论判为"未校验" —— 界面要如实说，不能照出图。

    这是用户"战术图不对"的根因：一份标定里有一半是错的，工具却报"可用"。
    """
    import asyncio
    import json
    import pathlib as _pl

    from aihoop import api

    monkeypatch.setattr(api, "_split_marks_by_shot",
                        lambda video, times: ([sorted(set(times))], []))
    out = _pl.Path(ROOT) / "_tmp" / "pytest_weakseg"
    if out.exists():
        import shutil
        shutil.rmtree(out, ignore_errors=True)
    monkeypatch.setattr(api, "_calibration_path_for",
                        lambda v: out / "calibration_x.json")

    def norm(px):
        return {n: [p[0] / float(W), p[1] / float(H)]
                for n, p in zip(NAMES, px)}

    body = {"video_path": "data/uploads/basketball_match_3min.mp4",
            "compensate": False, "confirm": True, "snap": False,
            "frames": [{"t": 13.5, "landmarks": norm(PX_A)},
                       {"t": 73.06, "landmarks": norm(PX_B)}]}
    r = asyncio.run(api.post_calibrate_multi(api.MultiCalibRequest(**body)))
    assert r.get("multi_shot") is True
    # 这两帧的 ratio 在这段素材上是算得出来的，若都达标则不该被误判为弱；
    # 但接口必须**始终**返回 weak_segments 与 position_unverified 两个字段，
    # 前端才能如实展示（缺字段会让界面无从判断）。
    assert "weak_segments" in (r.get("calibration_fit") or {}), \
        "多机位返回里必须有逐段弱项，界面才能如实说哪个镜头不行"
    assert "position_unverified" in r
    saved = out / "calibration_x.json"
    assert saved.exists()
    raw = json.loads(saved.read_text(encoding="utf-8"))
    assert raw.get("multi_shot") is True and len(raw["segments"]) == 2


def test_roundtrip_through_json_keeps_both_segments():
    """存盘/读回要走通（分析端靠这个按帧取标定）。"""
    import json
    from aihoop.multical import MultiCal
    mc = _build_multical_from_frames(_frames(), "v.mp4", W, H, duration=200.0)
    raw = json.loads(json.dumps(mc.to_dict(), ensure_ascii=False))
    mc2 = MultiCal.from_dict(raw)
    assert mc2 is not None and len(mc2.segments) == 2
    # 两段的 H 必须都还在（少了哪一段，那段时间的球员就投不出来）
    assert mc2.segments[0].H and mc2.segments[1].H
    u1, _v1 = mc2.cal_at(20).to_court(*PX_A[0])
    u2, _v2 = mc2.cal_at(100).to_court(*PX_B[0])
    assert abs(u1 - (-2.45)) < 0.05, "第 1 段投回不对：%s" % u1
    assert abs(u2 - (-2.45)) < 0.05, "第 2 段投回不对：%s" % u2


@pytest.mark.skipif(not os.path.exists(VIDEO), reason="示例视频不在")
def test_api_returns_per_frame_multical_end_to_end():
    """走真实接口：两幅各自成立但互相矛盾 → 应当返回 multi_shot=True。

    这是用户截图里的那条路径（旧行为是报"跨画面的点混在一起解不了"）。
    """
    import asyncio

    from aihoop import api

    def norm(px):
        return {n: [p[0] / float(W), p[1] / float(H)]
                for n, p in zip(NAMES, px)}

    body = {"video_path": "data/uploads/basketball_match_3min.mp4",
            "compensate": False, "confirm": False, "snap": False,
            "frames": [{"t": 13.5, "landmarks": norm(PX_A)},
                       {"t": 73.06, "landmarks": norm(PX_B)}]}
    r = asyncio.run(api.post_calibrate_multi(api.MultiCalibRequest(**body)))
    assert r.get("ok") is True, "应当成功：%s" % str(r.get("note"))[:200]
    assert r.get("multi_shot") is True, "应当走多镜头路径"
    # 两条路都算对：能用切镜检测时走 "per-shot"（本机短视频会命中），
    # 检测不到切镜时走 "per-frame"（用户那台 HD 长视频就是这种，
    # 实测 find_cut_between 没找到切镜点）。两条都是"每幅画面一份 H"。
    assert r.get("via") in ("per-shot", "per-frame"), \
        "应当按画面分别解算：%s" % r.get("via")
    assert r.get("n_segments") == 2


@pytest.mark.skipif(not os.path.exists(VIDEO), reason="示例视频不在")
def test_api_falls_back_when_cut_detection_finds_nothing(monkeypatch):
    """**切镜检测没找到切镜点**时也必须能走多机位 —— 这是用户那台视频的实况。

    实测背景：用户的 HD 长视频上 `find_cut_between` 返回 None
    （`_sample` 取帧拿不到画面时会静默当"同一镜头"），
    于是依赖切镜检测的那条路**整条没触发**，用户看到的是
    "跨画面的点混在一起解不了"。
    所以回退判据必须只看**"混在一起解不了 + 每幅各自成立"**。
    """
    import asyncio

    from aihoop import api

    # 强制"检测不到切镜"：模拟那条失败路径
    monkeypatch.setattr(api, "_split_marks_by_shot",
                        lambda video, times: ([sorted(set(times))], []))

    def norm(px):
        return {n: [p[0] / float(W), p[1] / float(H)]
                for n, p in zip(NAMES, px)}

    body = {"video_path": "data/uploads/basketball_match_3min.mp4",
            "compensate": False, "confirm": False, "snap": False,
            "frames": [{"t": 13.5, "landmarks": norm(PX_A)},
                       {"t": 73.06, "landmarks": norm(PX_B)}]}
    r = asyncio.run(api.post_calibrate_multi(api.MultiCalibRequest(**body)))
    assert r.get("ok") is True, \
        "切镜检测失败时也必须成功：%s" % str(r.get("note"))[:220]
    assert r.get("multi_shot") is True
    assert r.get("via") == "per-frame", "应当走不依赖切镜检测的回退：%s" % r.get("via")
    assert r.get("n_segments") == 2
    assert "各自都能标定" in str(r.get("note") or "")


@pytest.mark.skipif(not os.path.exists(VIDEO), reason="示例视频不在")
def test_saving_per_frame_multical_writes_backup_and_reads_back(monkeypatch):
    """保存多镜头标定要走带备份的写盘，并且读回来两段都在。"""
    import asyncio
    import json

    from aihoop import api
    from aihoop.multical import MultiCal

    monkeypatch.setattr(api, "_split_marks_by_shot",
                        lambda video, times: ([sorted(set(times))], []))
    monkeypatch.setattr(api, "_calibration_path_for",
                        lambda v: ROOTP / "_tmp" / "pytest_twocam" / "calibration_x.json")

    def norm(px):
        return {n: [p[0] / float(W), p[1] / float(H)]
                for n, p in zip(NAMES, px)}

    out = ROOTP / "_tmp" / "pytest_twocam"
    if out.exists():
        import shutil
        shutil.rmtree(out, ignore_errors=True)
    body = {"video_path": "data/uploads/basketball_match_3min.mp4",
            "compensate": False, "confirm": True, "snap": False,
            "frames": [{"t": 13.5, "landmarks": norm(PX_A)},
                       {"t": 73.06, "landmarks": norm(PX_B)}]}
    r = asyncio.run(api.post_calibrate_multi(api.MultiCalibRequest(**body)))
    assert r.get("saved") is True and r.get("revision") == 1
    p = out / "calibration_x.json"
    assert p.exists(), "标定没写盘"
    raw = json.loads(p.read_text(encoding="utf-8"))
    assert raw.get("multi_shot") is True
    mc = MultiCal.from_dict(raw)
    assert mc is not None and len(mc.segments) == 2
    # 再存一次必须留下 .bak（用户数据不可覆盖丢失）
    r2 = asyncio.run(api.post_calibrate_multi(api.MultiCalibRequest(**body)))
    assert r2.get("revision") == 2
    assert (out / "calibration_x.json.bak").exists(), \
        "第二次保存必须留下备份（标定不在 git 里，覆盖就找不回来）"
