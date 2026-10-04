"""套餐 B（战术层）自检 —— 零第三方依赖，可直接运行：

    python -B tests/test_plan_b.py

覆盖：
  1. 标定坐标系回归（**这一条是真实 bug**：老版本半场标定把底线放在 |y|=14，
     导致篮下上篮被算成离篮筐 12.4m 的三分，俯视战术图整体偏移 14m）
  2. 折半坐标（全场 -> 分析坐标）自反性与篮筐对齐
  3. 纯几何工具：凸包面积、平均间距、空间指标
  4. 阵型识别：五外 / 四外一内 / 三外两内 / 双塔 / 转换进攻
  5. 防守识别：人盯人 / 2-3 / 3-2 / 1-3-1 / 前场施压
  6. 控球归属 + 传球检测 + 传球网络
  7. 合成数据的球员轨迹形状与"幽灵球员"回归
  8. 管线端到端产出 tactics.json / tactics_frames.jsonl / passes.csv / spacing.csv
  9. 退化路径：没有球员轨迹时必须 available=False 并给出原因
 10. 空间曲线抽稀必须**两队都在**（真实 bug：整体抽稀会与球队交替同频）
"""
from __future__ import annotations

import json
import math
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from aihoop.court import (  # noqa: E402
    Calibration, calibrate_from_corners, fold_to_analysis, apply_homography,
)
from aihoop.model import COURT_LENGTH, COURT_WIDTH, HOOP_LEFT, HOOP_RIGHT  # noqa: E402
from aihoop.rules import BallSample, detect_rim_events, shot_value  # noqa: E402
from aihoop.tactics import (  # noqa: E402
    PlayerIndex, PlayerSample, TacticsConfig, attack_frame, build_tactics,
    build_tactics_frames, classify_defense, classify_offense,
    convex_hull_area, detect_passes, detect_possessions, group_possessions,
    mean_pairwise_distance, pass_network, spacing_metrics,
)


# --------------------------------------------------------------------------
# 1) 标定坐标系（回归）
# --------------------------------------------------------------------------
def _fake_camera(scale: float = 40.0, ox: float = 300.0, oy: float = 800.0):
    """造一台"正对半场的俯视相机"：分析坐标 -> 像素（纯仿射）。

    分析坐标里左半场 y ∈ [-14, 0]，且 |y| 是**离底线的距离**（底线 = 0）。
    于是像素 py = oy - scale*y：底线(y=0) 在最下方，中圈(y=-14) 往画面里走。
    """
    def to_px(x: float, y: float):
        return (ox + scale * x, oy - scale * y)
    return to_px


def test_calibration_layup_is_two_points():
    """半场四角标定之后，篮下的球必须被判成 2 分（而不是三分）。

    这是套餐 B 的地基：俯视战术图 / 空间指标全部依赖"标定出来的坐标"，
    坐标系错了，战术图会整体偏移 14m，而且**看起来还挺像那么回事**。
    """
    from aihoop.court import HALF_COURT_CORNERS
    to_px = _fake_camera()
    corners = [to_px(*c) for c in HALF_COURT_CORNERS]
    cal = calibrate_from_corners(corners, half_court=True,
                                 video_path="t.mp4", frame_size=[1280, 960])
    assert cal.frame == "half"
    assert cal.reproj_error_m < 1e-6, cal.reproj_error_m

    # ① 篮筐本身：投影回来必须正好是 (0, -1.575)
    bx, by = cal.to_court(*to_px(0.0, HOOP_LEFT[1]))
    assert abs(bx) < 1e-6 and abs(by - HOOP_LEFT[1]) < 1e-6, (bx, by)

    # ② 篮下上篮 = 2 分；弧顶 6.9m = 3 分；底角 6.8m = 3 分
    for (x, y), want in [((0.4, -2.4), 2), ((0.0, -8.5), 3),
                         ((6.8, -2.0), 3), ((0.0, -5.8), 2)]:
        gx, gy = cal.to_court(*to_px(x, y))
        got = shot_value(gx, gy)
        assert got == want, f"({x},{y}) -> ({gx:.2f},{gy:.2f}) 判成 {got} 分，应为 {want}"

    # ③ 往返：分析坐标 -> 像素 -> 分析坐标
    for pt in [(3.0, -2.0), (-6.5, -7.0), (0.0, -13.5)]:
        back = cal.to_court(*cal.to_pixel(*pt))
        assert abs(back[0] - pt[0]) < 1e-6 and abs(back[1] - pt[1]) < 1e-6, back


def test_full_court_calibration_folds_to_analysis():
    """全场标定（未折半坐标）必须折成分析坐标：两条底线 -> 各自的 |y|=0。"""
    from aihoop.court import FULL_COURT_CORNERS
    def to_px(x, y):                      # 全场：y ∈ [-14,14]
        return (200 + 30 * (x + 7.5), 100 + 30 * (y + 14))
    cal = calibrate_from_corners([to_px(*c) for c in FULL_COURT_CORNERS],
                                 half_court=False)
    assert cal.frame == "full"
    # 右篮筐：全场 y = 14 - 1.575 = 12.425 -> 分析 y = +1.575
    gx, gy = cal.to_court(*to_px(0.0, 12.425))
    assert abs(gy - HOOP_RIGHT[1]) < 1e-6, gy
    # 左篮筐 -> 分析 y = -1.575
    gx, gy = cal.to_court(*to_px(0.0, -12.425))
    assert abs(gy - HOOP_LEFT[1]) < 1e-6, gy
    # 折半函数自反（画图时"分析 -> 全场 -> 像素"复用同一个函数）
    for pt in [(0.0, 12.0), (3.0, -9.0), (0.0, 0.0)]:
        assert fold_to_analysis(*fold_to_analysis(*pt)) == pt


def test_attack_frame_unifies_both_halves():
    """局部进攻坐标：攻左/攻右的同一个位置必须得到同一个 (x, d)。"""
    assert attack_frame(3.0, -5.0) == (3.0, 5.0)
    assert attack_frame(3.0, 5.0) == (3.0, 5.0)


# --------------------------------------------------------------------------
# 2) 几何工具
# --------------------------------------------------------------------------
def test_convex_hull_and_pairwise():
    assert abs(convex_hull_area([(0, 0), (1, 0), (1, 1), (0, 1)]) - 1.0) < 1e-9
    assert abs(convex_hull_area([(0, 0), (2, 0), (0, 2)]) - 2.0) < 1e-9
    assert convex_hull_area([(0, 0), (1, 1)]) == 0.0        # 少于 3 点
    assert convex_hull_area([(1, 1), (1, 1), (1, 1)]) == 0.0  # 退化成一点
    assert abs(mean_pairwise_distance([(0, 0), (3, 4)]) - 5.0) < 1e-9


def test_spacing_metrics_are_monotonic():
    """拉得越开：凸包面积、平均间距、宽度都应该更大。"""
    tight = [(-1, -2), (1, -2), (0, -3), (-1, -1.6), (1, -1.6)]
    wide = [(-6.5, -2), (6.5, -2), (0, -8), (-5, -6), (5, -6)]
    a, b = spacing_metrics(tight), spacing_metrics(wide)
    for k in ("area", "mean_dist", "width"):
        assert b[k] > a[k], (k, a[k], b[k])


# --------------------------------------------------------------------------
# 3) 阵型识别
# --------------------------------------------------------------------------
def test_classify_offense_shapes():
    """用合成数据的落位模板反查：分类必须能认出模板本身。"""
    from aihoop.sources import SYNTH_OFFENSE_SETS
    for name, spots in SYNTH_OFFENSE_SETS.items():
        got = classify_offense(spots)["shape"]
        assert got == name, f"模板 {name} 被判成 {got}"
    # 有人还在中圈附近 -> 转换进攻
    assert classify_offense([(-6, 1.6), (6, 1.6), (0, 13.5),
                             (-2, 11), (2, 11)])["shape"] == "转换进攻"


def test_classify_defense_schemes():
    atk = [(-6, 1.6), (6, 1.6), (0, 6.5), (-2.5, 2.5), (2.5, 2.5)]

    def man(spots):
        """人盯人：每名防守人都贴在进攻人身边（朝篮筐方向 1m）。"""
        out = []
        for x, d in spots:
            vx, vd = 0.0 - x, 1.575 - d
            n = math.hypot(vx, vd) or 1.0
            out.append((x + vx / n * 1.0, d + vd / n * 1.0))
        return out

    cases = {
        "2-3 联防": [(-2.6, 6.2), (2.6, 6.2), (-5.6, 1.9), (0.6, 1.7), (5.6, 1.9)],
        "3-2 联防": [(-4.2, 6.3), (0.0, 6.7), (4.2, 6.3), (-3.0, 2.0), (3.0, 2.0)],
        "1-3-1 联防": [(0.0, 7.7), (-4.4, 4.7), (0.0, 4.5), (4.4, 4.7), (0.0, 1.8)],
        "人盯人": man(atk),
        "前场施压（紧逼）": [(-2, 12.0), (2, 12.0), (-2, 4.0), (0, 10.5), (2, 4.0)],
    }
    for want, spots in cases.items():
        got = classify_defense(spots, atk)["scheme"]
        assert got == want, f"{want} 被判成 {got}"
    # 判据必须可解释：把原始量一起返回
    r = classify_defense(cases["2-3 联防"], atk)
    assert (r["top"], r["low"]) == (2, 3) and "man_gap" in r


def test_defense_reports_reason_metrics():
    atk = [(-4, 5), (4, 5), (0, 2), (-6, 2), (6, 2)]
    r = classify_defense([(-2, 6), (2, 6), (-5, 2), (0, 1.6), (5, 2)], atk)
    for k in ("top", "mid", "low", "man_gap", "press", "mean_rim_dist", "confidence"):
        assert k in r, k


# --------------------------------------------------------------------------
# 4) 控球 / 传球
# --------------------------------------------------------------------------
def _make_scene(seq, dt=0.25):
    """seq: [(t0, t1, player_id, (x, y)), ...] -> (player_samples, ball_samples)

    其余球员固定站在远处，保证"最近的球员"一定是持球人。
    """
    from aihoop.model import Player
    players = {
        "H1": Player("H1", "主队1号", "home"),
        "H2": Player("H2", "主队2号", "home"),
        "A1": Player("A1", "客队1号", "away"),
    }
    pos = {"H1": [-3, -3], "H2": [3, -3], "A1": [0, -6]}
    samples, balls = [], []
    for (t0, t1, pid, _xy) in seq:
        t = t0
        while t <= t1 + 1e-9:
            for k, (px, py) in pos.items():
                samples.append(PlayerSample(t=t, player_id=k,
                                            team=players[k].team, x=px, y=py))
            balls.append(BallSample(t=t, x=pos[pid][0], y=pos[pid][1],
                                    z=1.2, conf=0.9, source="possession"))
            t += dt
    samples.sort(key=lambda s: s.t)
    balls.sort(key=lambda b: b.t)
    return players, samples, balls


def test_possession_and_pass_detection():
    players, samples, balls = _make_scene([
        (0.0, 3.0, "H1", None),
        (3.0, 6.0, "H2", None),
        (6.0, 9.0, "A1", None),
    ])
    cfg = TacticsConfig(frame_step=0.25, min_possession=0.4, max_gap=1.5)
    idx = PlayerIndex(samples, cfg)
    spells = detect_possessions(balls, idx, cfg)
    got = [(round(s["t0"]), round(s["t1"]), s["player_id"]) for s in spells]
    assert got == [(0, 3, "H1"), (3, 6, "H2"), (6, 9, "A1")], got

    passes = detect_passes(spells, idx, cfg)
    kinds = [(p["from"], p["to"], p["kind"]) for p in passes]
    assert kinds == [("H1", "H2", "pass"), ("H2", "A1", "turnover")], kinds
    net = pass_network(passes, players, {k: (0.0, 3.0) for k in players})
    edges = {(e["source"], e["target"]): e["value"] for e in net["edges"]}
    assert edges == {("H1", "H2"): 1}, edges


def test_group_possessions_merges_teammate_touches():
    """一次进攻里传了 3 脚 = 4 个控球片段，但只应算 1 个回合。"""
    spells = [{"t0": 0, "t1": 2, "team": "home", "player_id": "H1"},
              {"t0": 2.5, "t1": 4, "team": "home", "player_id": "H2"},
              {"t0": 4.5, "t1": 6, "team": "home", "player_id": "H3"},
              {"t0": 30, "t1": 32, "team": "home", "player_id": "H1"}]
    passes = [{"t": 2.2, "team": "home", "from": "H1", "to": "H2", "kind": "pass"},
              {"t": 4.2, "team": "home", "from": "H2", "to": "H3", "kind": "pass"}]
    poss = group_possessions(spells, passes, [])
    assert len(poss) == 2, poss
    assert poss[0]["passes"] == 2 and poss[0]["touches"] == 3
    assert poss[0]["t0"] == 0 and poss[0]["t1"] == 6


def test_rim_events_ignore_possession_samples():
    """持球段（source=possession）不能参与"球穿筐"判定 —— 否则篮下会凭空打铁。"""
    # 一段"球停在篮筐正下方"的持球采样
    poss = [BallSample(t=0.0, x=0.0, y=1.6, z=1.2, source="possession"),
            BallSample(t=0.1, x=0.0, y=1.6, z=1.2, source="possession")]
    assert detect_rim_events(poss) == []
    # 同样位置，但标成飞行段就会产生候选（说明过滤真的起作用）
    flight = [BallSample(t=0.0, x=0.0, y=1.6, z=3.4, source="flight"),
              BallSample(t=0.1, x=0.0, y=1.6, z=1.2, source="flight")]
    assert len(detect_rim_events(flight)) == 1


# --------------------------------------------------------------------------
# 5) 合成轨迹 + 端到端
# --------------------------------------------------------------------------
def test_synthetic_player_track_has_no_ghost_players():
    from aihoop.sources import synthetic_game
    rt = synthetic_game(seed=7, duration=600.0)
    assert rt.player_track, "合成数据必须自带球员轨迹（套餐 B 的演示依赖它）"
    ids = {s.player_id for s in rt.player_track}
    assert ids == set(rt.players), f"球员轨迹里出现了不存在的球员：{ids - set(rt.players)}"
    teams = {s.team for s in rt.player_track}
    assert teams == {"home", "away"}
    # 坐标必须落在合理范围内（分析坐标：|x|<=7.5，|y|<=14 多一点余量）
    for s in rt.player_track:
        assert abs(s.x) <= 8.0 and abs(s.y) <= 14.5, (s.player_id, s.x, s.y)
    # 球轨迹要同时包含持球段与飞行段
    srcs = {b.source for b in rt.ball_track}
    assert "possession" in srcs and "flight" in srcs, srcs


def test_tactics_degrades_without_player_track():
    from aihoop.sources import synthetic_game
    rt = synthetic_game(seed=7, duration=300.0)
    t = build_tactics([], rt.ball_track, rt.players, rt.attempts,
                      duration=rt.duration)
    assert t["available"] is False
    assert "球员轨迹" in t["reason"]


# --------------------------------------------------------------------------
# 5b) 回归：退化标定 + 自动逐帧标定出战术图
# --------------------------------------------------------------------------
def test_degenerate_calibration_is_detected():
    """近共线的点位必须被判成"退化"。

    这是真实踩到的坑：用户在界面上标了 4 个点，**4 个点的 y 都在 310~325px**
    （几乎一条横线）。4 个点时单应矩阵是精确解 → 重投影误差 1.45m、"标定可用"，
    而画面里的篮筐被投到 (-31.0, -14.0)、离真篮筐 **33.4m**，热区与俯视战术图
    整片错位且不报任何错。**误差小不能证明标定对**，所以要单独判"点位展开度"。
    """
    from aihoop.court import calibration_degeneracy
    bad = Calibration(
        src_px=[[448.8, 325.1], [955.9, 310.2], [720.2, 321.6], [275.1, 318.1]],
        dst_m=[[-7.5, 0.0], [7.5, 0.0], [0.0, 0.0], [0.0, -5.8]],
        H=[[1.0, 0.0, 0.0], [0.0, 1.0, 0.0], [0.0, 0.0, 1.0]],
        reproj_error_m=1.45, frame="full", frame_size=[960, 544])
    d = calibration_degeneracy(bad)
    assert d["degenerate"] is True, d
    assert "同一条线" in d["reason"] or "共线" in d["reason"], d["reason"]

    # 点重合也要判出来
    dup = Calibration(src_px=[[100, 100], [100.5, 100.2], [900, 90], [100, 700]],
                      dst_m=[[-7.5, 0.0], [7.5, 0.0], [0.0, 0.0], [0.0, -5.8]],
                      H=[[1.0, 0.0, 0.0], [0.0, 1.0, 0.0], [0.0, 0.0, 1.0]],
                      frame_size=[960, 544])
    assert calibration_degeneracy(dup)["degenerate"] is True

    # 一份正常的四角标定不能被误判
    to_px = _fake_camera()
    src = [list(to_px(x, y)) for x, y in
           [(-7.5, 0.0), (7.5, 0.0), (7.5, -14.0), (-7.5, -14.0)]]
    good = calibrate_from_corners(src, half_court=True)
    good.frame_size = [960, 544]
    assert calibration_degeneracy(good)["degenerate"] is False, \
        calibration_degeneracy(good)


def _rt_with_sliding(ok_ratio: float, median_ratio: float):
    """造一份"静态标定不可用、改用自动逐帧标定"的 RawTrack（真视频的兜底路径）。"""
    from aihoop.sources import synthetic_game
    rt = synthetic_game(seed=7, duration=300.0)
    rt.detections_meta["calibration_valid"] = False
    rt.detections_meta["calibration_method"] = "web-keypoints-multi"
    rt.detections_meta["calibration_rmse_m"] = 1.45
    rt.detections_meta["player_track_source"] = "sliding_calibration"
    rt.detections_meta["player_track"] = {
        "samples": len(rt.player_track),
        "players": len({s.player_id for s in rt.player_track}),
        "calibration_valid": False}
    rt.detections_meta["sliding_calibration"] = {
        "ok_ratio": ok_ratio, "median_ratio": median_ratio,
        "half_court": True}
    rt.detections_meta["sliding_anchors"] = 1201
    return rt


def test_tactics_runs_on_sliding_calibration(tmp_path: Path):
    """静态标定不可用时，**自动逐帧标定**的球员坐标要能出战术图（并标注未校验）。

    真实痛点：真视频里用户那份标定经常通不过校验（机位在动 / 标定点位错），
    而逐帧滑动标定其实已经把球员投成了球场坐标（实测 2.4 万个点、达标率 0.958）。
    老版本只看静态标定的结论 → 战术层整层跳过，用户看到的就是"球员明明检测到了、
    战术页却一直空着"，而且完全不知道为什么。
    """
    from aihoop.pipeline import PipelineConfig, run_pipeline
    out = tmp_path / "sliding_ok"
    rt = _rt_with_sliding(ok_ratio=0.958, median_ratio=3.09)
    res = run_pipeline(rt, PipelineConfig(out_dir=str(out),
                                          make_highlights=False))
    assert (out / "tactics.json").exists(), "自动逐帧标定达标时必须产出战术图"
    t = json.loads((out / "tactics.json").read_text(encoding="utf-8"))
    assert t["available"] is True
    # 位置来源要如实标注：不能冒充"已校验的标定"
    assert t["position_unverified"] is True
    assert t["position_source"] == "sliding_calibration"
    assert "未校验" in t["position_note"] or "自动逐帧标定" in t["position_note"]
    assert any("未校验" in n for n in t["notes"]), t["notes"]
    assert res.game["tactics"]["position_unverified"] is True
    # 战报里必须出现这条免责说明（答辩看的是报告）
    md = (out / "report.md").read_text(encoding="utf-8")
    assert "未独立校验" in md, md[-1200:]


def test_tactics_skipped_when_sliding_too_weak(tmp_path: Path):
    """自动逐帧标定质量不够时**不能**硬出战术图 —— 明确说明原因。"""
    from aihoop.pipeline import PipelineConfig, run_pipeline
    out = tmp_path / "sliding_bad"
    rt = _rt_with_sliding(ok_ratio=0.12, median_ratio=1.0)
    res = run_pipeline(rt, PipelineConfig(out_dir=str(out),
                                          make_highlights=False))
    assert not (out / "tactics.json").exists()
    tac = res.game["tactics"]
    assert tac["available"] is False
    assert "自动逐帧标定" in tac["reason"], tac


def test_validate_sliding_with_hoop():
    """自动逐帧标定必须能被**人工标的篮筐**一票否决（真实踩到的坑）。

    实测这段 960×544 校园转播：滑动标定把手标篮筐投到离真筐 **9.2m** 的地方，
    而它自己的质量分 ratio 反而高达 7.17（比"看着还行"的锚点 1.4~2.9 还高）——
    也就是说线拟合分**不能**证明坐标对。只有与拟合无关的真值点（手标篮筐）
    才能发现它，所以战术层必须以这道校验为准。
    """
    from aihoop.calibcheck import validate_sliding_with_hoop
    from aihoop.court import HALF_COURT_CORNERS
    from aihoop.model import HOOP_LEFT

    to_px = _fake_camera(scale=40.0, ox=300.0, oy=800.0)
    corners = [list(to_px(x, y)) for x, y in HALF_COURT_CORNERS]
    sliding = {"anchors": [{"frame": 0, "corners": corners, "ratio": 3.0}],
               "fps": 30.0, "half_court": True}
    # 真值点：分析坐标 (0, 1.575) 对应的像素
    hoop_px = to_px(HOOP_LEFT[0], HOOP_LEFT[1])
    good = validate_sliding_with_hoop(sliding, hoop_px, t=0.0)
    assert good["checked"] is True, good
    assert good["ok"] is True, good
    assert good["dist_m"] is not None and good["dist_m"] < 0.5, good

    # 同一个真值点，但标定整体偏了 300px（≈7.5m）—— 必须判不通过
    shifted = {"anchors": [{"frame": 0,
                            "corners": [[x + 300, y] for x, y in corners],
                            "ratio": 9.9}],       # ratio 很高也没用
               "fps": 30.0, "half_court": True}
    bad = validate_sliding_with_hoop(shifted, hoop_px, t=0.0)
    assert bad["checked"] is True and bad["ok"] is False, bad
    assert bad["dist_m"] > 3.0, bad
    assert "不能用" in bad["reason"], bad["reason"]

    # 没有真值点时必须明确"没校验"，而不是默认通过
    none = validate_sliding_with_hoop(sliding, None, t=0.0)
    assert none["checked"] is False and none["ok"] is False, none


def test_tactics_skipped_when_sliding_unvalidated(tmp_path: Path):
    """滑动标定没过独立校验时，战术层必须**不出图**并说清原因。"""
    from aihoop.pipeline import PipelineConfig, run_pipeline
    out = tmp_path / "sliding_unvalidated"
    rt = _rt_with_sliding(ok_ratio=0.958, median_ratio=3.09)
    # 校验结论：手标篮筐被投到 9.2m 外 → 不可用
    rt.detections_meta["sliding_validation"] = {
        "checked": True, "ok": False, "dist_m": 9.2,
        "reason": "人工标的篮筐经这份逐帧标定投到 (0.0, 11.2)，离真篮筐 9.20 m"}
    res = run_pipeline(rt, PipelineConfig(out_dir=str(out),
                                          make_highlights=False))
    assert not (out / "tactics.json").exists(), "校验没过就不该出战术图"
    tac = res.game["tactics"]
    assert tac["available"] is False
    assert "独立校验" in tac["reason"], tac


def test_pipeline_writes_tactics_artifacts(tmp_path: Path):
    from aihoop.pipeline import PipelineConfig, run_pipeline
    from aihoop.sources import synthetic_game
    rt = synthetic_game(seed=11, duration=900.0)
    res = run_pipeline(rt, PipelineConfig(out_dir=str(tmp_path / "o"),
                                          make_highlights=False))
    out = tmp_path / "o"
    for name in ("tactics.json", "tactics_frames.jsonl", "passes.csv", "spacing.csv"):
        assert (out / name).exists(), f"缺少产物 {name}"

    t = json.loads((out / "tactics.json").read_text(encoding="utf-8"))
    assert t["available"] is True
    assert t["possession"]["total"] > 0
    assert t["passes"]["total"] > 0
    assert any(v["edges"] for v in t["pass_network"].values())
    # game.json 里要有战术摘要（总览页/侧边栏用），但**不能**塞进逐帧数据
    assert res.game["tactics"]["available"] is True
    assert len(json.dumps(res.game["tactics"], ensure_ascii=False)) < 4000
    # report.json 里也不能塞整份战术数据
    rj = json.loads((out / "report.json").read_text(encoding="utf-8"))
    assert rj["tactics"]["available"] is True
    assert "events" not in (rj["tactics"].get("passes") or {})


def test_tactics_end_to_end_and_frames():
    from aihoop.sources import synthetic_game
    rt = synthetic_game(seed=7, duration=600.0)
    cfg = TacticsConfig()
    t = build_tactics(rt.player_track, rt.ball_track, rt.players, rt.attempts,
                      duration=rt.duration, cfg=cfg)
    assert t["available"] is True
    # 阵型：两队都要有攻防片段
    for side in ("home", "away"):
        assert t["formation"]["offense"][side], side
        assert t["formation"]["defense"][side], side
    # 传球网络节点只能落在真实球员上
    real = set(rt.players)
    for side in ("home", "away"):
        for n in t["pass_network"][side]["nodes"]:
            assert n["id"] in real, n
    # 空间指标必须量级合理（半场进攻：面积 5~120 m²，间距 1~12 m）
    for side in ("home", "away"):
        d = t["spacing"]["teams"][side]
        assert 5 < d["area"] < 120, d
        assert 1 < d["mean_dist"] < 12, d
    # 逐帧数据
    frames = build_tactics_frames(rt.player_track, rt.ball_track,
                                  duration=rt.duration, cfg=cfg)
    assert frames
    for f in frames[:200]:
        assert f["players"], f
        for pid, team, x, y in f["players"]:
            assert pid in real and team in ("home", "away")
            assert abs(x) <= 8.0 and abs(y) <= 14.5


def test_spacing_timeline_keeps_both_teams():
    """回归：空间曲线抽稀**不能**退化成只剩一支球队。

    spacing_timeline 是 home/away 交替排列的；整体 [::2] 抽稀会与交替同频，
    结果只剩一队 —— 而且不报任何错，曲线看着还挺正常。
    """
    from aihoop.sources import synthetic_game
    rt = synthetic_game(seed=7, duration=900.0)
    cfg = TacticsConfig(frame_step=1.0, max_spacing_points=300)
    t = build_tactics(rt.player_track, rt.ball_track, rt.players, rt.attempts,
                      duration=rt.duration, cfg=cfg)
    rows = t["spacing"]["timeline"]
    n_home = sum(1 for r in rows if r["team"] == "home")
    n_away = sum(1 for r in rows if r["team"] == "away")
    assert n_home > 0 and n_away > 0, (n_home, n_away)
    assert abs(n_home - n_away) <= 2, (n_home, n_away)
    assert len(rows) <= 300 + 4, len(rows)


# --------------------------------------------------------------------------
# 让脚本可以不用 pytest 直接运行
# --------------------------------------------------------------------------
def _run_all() -> int:
    import inspect
    import shutil
    import traceback

    fns = [(n, f) for n, f in sorted(globals().items())
           if n.startswith("test_") and callable(f)]
    base = ROOT / "out" / "_selftest_b"
    if base.exists():
        shutil.rmtree(base, ignore_errors=True)
    base.mkdir(parents=True, exist_ok=True)
    ok = fail = 0
    for name, fn in fns:
        params = inspect.signature(fn).parameters
        d = base / name
        d.mkdir(parents=True, exist_ok=True)
        try:
            if "tmp_path" in params:
                fn(d)
            else:
                fn()
            print(f"  PASS  {name}")
            ok += 1
        except Exception as e:  # noqa: BLE001
            print(f"  FAIL  {name}: {type(e).__name__}: {e}")
            traceback.print_exc()
            fail += 1
        finally:
            shutil.rmtree(d, ignore_errors=True)
    shutil.rmtree(base, ignore_errors=True)
    print(f"\n自检结果：{ok} 通过 / {fail} 失败（共 {ok + fail} 项）")
    return 1 if fail else 0


if __name__ == "__main__":
    raise SystemExit(_run_all())