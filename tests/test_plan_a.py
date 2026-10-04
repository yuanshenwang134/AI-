"""端到端自检 —— 不装任何重依赖也能跑（pytest 或直接 python 运行）。

覆盖套餐 A 的 6 个验收项：
  1. 自动计分（含 2/3 分区分）   2. 球员统计   3. 命中率
  4. 投篮热区                    5. 高光片段   6. 战报与导出
"""
from __future__ import annotations

import json
import math
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from aihoop.model import (  # noqa: E402
    COURT_LENGTH, COURT_WIDTH, HOOP_LEFT, HOOP_RIGHT, Shot, ShotValue,
)
from aihoop.rules import (  # noqa: E402
    Evidence, RulesConfig, build_shots, compute_player_stats, compute_team_stats,
    detect_rim_events, fuse_outcome, is_three_pointer, possessions,
    quarter_scores, score_progression, shot_chart, shot_value, zone_of,
)
from aihoop.court import (  # noqa: E402
    apply_homography, calibrate_from_corners, find_homography, invert,
)
from aihoop.sources import synthetic_game  # noqa: E402


# --------------------------------------------------------------------------
# 1) 三分线几何：这是计分正确性的根基，必须重点测
# --------------------------------------------------------------------------
def test_three_point_arc():
    """三分线几何：用 tolerance=0 测纯几何，再单独测容差行为。

    坐标系：x 横向（宽 ±7.5），y 纵向（长 ±14），篮筐在 (0, ±1.575)。
    所以「距篮筐 d 米、横向偏移 x」的点是 (x, HOOP_RIGHT[1] + sqrt(d²-x²))。
    """
    G = RulesConfig(three_pt_tolerance=0.0)   # 纯几何，边界清晰
    HY = HOOP_RIGHT[1]

    def three(x, y, cfg=G):
        return is_three_pointer(x, y, cfg=cfg)

    def at(d, lateral=0.0):
        """构造距篮 d 米、横向偏移 lateral 的出手点（右半场）。"""
        return (lateral, HY + math.sqrt(max(0.0, d * d - lateral * lateral)))

    # --- 弧线段（半径 6.75m），弧顶 ---
    assert three(*at(6.9)) is True, "弧顶 6.9m 应为三分"
    assert three(*at(6.5)) is False, "弧顶 6.5m 应为两分"
    # 注：浮点误差会让 hypot(6.75) 算出 6.749999999999999，
    # 所以边界值用 ±1mm 来测，避免把浮点噪声误当成几何规则错误。
    assert three(*at(6.751)) is True, "纯几何下刚出线算三分"
    assert three(*at(6.749)) is False, "纯几何下刚在线内算两分"

    # --- 底角直线段：距边线 0.90m，即 |x| >= 6.60（横向！）---
    # 底角三分：横向 6.8m（距边线 0.7m），纵向在靠近底线的位置
    assert three(6.8, HY + 2.0) is True, "横向贴边线的底角应为三分"
    assert three(-6.8, HOOP_LEFT[1] - 2.0) is True, "左侧底角镜像也应为三分"
    # 同样贴边线但更靠近篮筐（距篮 5.0m）-> 两分，不能被底角规则误判
    assert three(4.0, HY + 3.0) is False, "禁区两翼的近距出手不该是三分"

    # --- 只认最近的篮筐（关键回归）---
    # 站在右篮筐正下方：到左篮筐 24.85m，但进攻的是右篮筐，只差 0.3m -> 两分
    assert three(0.0, HY + 0.3) is False, "不许因为离对面篮筐远就误判三分"

    # --- 左右半场对称 ---
    for x, y in [(5.0, 5.0), (6.9, 2.0), (6.8, 3.0), (1.0, 8.0)]:
        assert is_three_pointer(x, y) == is_three_pointer(-x, -y), (x, y)

    # --- 容差行为：默认 0.05m，方向是「向场内收缩」（踩线算 2 分）---
    default = RulesConfig()
    assert is_three_pointer(*at(6.72), cfg=default) is True, \
        "6.72m 距线内 3cm，超出容差，仍算三分"
    # 容差调大后，落在容差带内的位置应被拉回两分
    # （距篮 6.50m，容差 0.20m -> 有效半径 6.55m，所以算两分）
    tight = RulesConfig(three_pt_tolerance=0.20)
    assert is_three_pointer(*at(6.50), cfg=tight) is False, \
        "距篮 6.50m、容差 0.20m 时应算两分"
    assert is_three_pointer(*at(6.60), cfg=tight) is True, \
        "距篮 6.60m、容差 0.20m 时应算三分"


def test_shot_value_and_zone():
    """分区口径 + 坐标系契约。

    这三个坐标系必须一致：计分引擎 / shot_chart 网格 / 前端半场绘制。
    约定：x 横向(±7.5)，y 纵向(±14)，篮筐 (0, ±1.575)。
    """
    HY = HOOP_RIGHT[1]

    assert shot_value(0.0, HY + 7.0) == 3          # 弧顶三分
    assert shot_value(0.0, HY + 1.0) == 2          # 篮下
    assert zone_of(0.0, HY + 1.0) == "禁区"
    assert zone_of(0.0, HY + 5.0) == "近距离中投"   # 距篮 5.0m（4.0~5.8m）
    assert zone_of(0.0, HY + 6.3) == "长两分"       # 距篮 6.3m（三分线内）
    assert zone_of(6.8, HY + 2.0) == "底角三分"
    assert zone_of(0.0, HY + 7.0) == "弧顶三分"
    assert zone_of(5.0, HY + 5.0) == "45°三分"

    # 关键回归：靠近右侧底线（离左侧篮筐很远）不能因为「离对面篮筐远」
    # 而被误判成三分 —— 三分只对被进攻的那个篮筐算距离
    assert is_three_pointer(0.0, HY + 0.3) is False, "篮下不该是三分"
    assert is_three_pointer(0.5, HY + 2.0) is False, "近距离不该是三分"


def test_axis_frame_consistency():
    """坐标系一致性回归 —— 曾经的真实 bug。

    历史问题：计分引擎把 x 当"距篮距离(纵向)"、y 当"横向"，
    而 shot_chart 的网格、court.HALF_COURT_CORNERS、前端 court.js
    都把 x 当横向、y 当纵向。结果底角三分的出手被堆到最后一行网格里，
    热区图被压扁；用标定角点算出的坐标喂给引擎还会把上篮判成三分。
    """
    from aihoop.court import HALF_COURT_CORNERS
    from aihoop.model import COURT_WIDTH

    # 1) 标定角点必须是 x 横向、y 纵向
    xs = [p[0] for p in HALF_COURT_CORNERS]
    ys = [p[1] for p in HALF_COURT_CORNERS]
    assert max(xs) - min(xs) == COURT_WIDTH, "四角点的横向跨度应为球场宽 15m"
    assert min(ys) == -14.0 and max(ys) == 0.0, "半场纵向应为 -14~0"

    # 2) 一个底角三分点，必须落在网格内且横向贴边线 —— 不能被 clamp 到最后一行
    from aihoop.model import Shot
    from aihoop.rules import shot_chart
    corner = Shot(t=10.0, team="home", player_id="H1",
                  x=6.8, y=HOOP_RIGHT[1] + 2.0, value=3, made=True)
    assert zone_of(corner.x, corner.y) == "底角三分"
    sc = shot_chart([corner])
    grid = sc["grid"]
    ny, nx = len(grid), len(grid[0])
    assert (ny, nx) == (14, 15), f"网格应为 纵向14行 × 横向15列，实际 {ny}x{nx}"
    cells = [(r, c) for r in range(ny) for c in range(nx) if grid[r][c]["att"]]
    assert len(cells) == 1, "只应有一个非空格"
    r, c = cells[0]
    # 横向 6.8m -> 第 14 列（最后一列，贴边线）；纵向约 3.6m -> 第 3 行
    assert c == 14, f"底角三分应落在最靠边线的那一列(14)，实际列 {c}"
    assert r == 3, f"纵向约 3.6m 应落在第 3 行，实际行 {r}"
    assert r != ny - 1, "底角三分不该被 clamp 到最后一行（这正是历史 bug）"

    # 3) 篮下出手必须被网格算在靠近底线的那几行，而不是贴边线
    layup = Shot(t=20.0, team="home", player_id="H1",
                 x=0.4, y=HOOP_RIGHT[1] + 0.8, value=2, made=True)
    sc2 = shot_chart([layup])
    cells2 = [(r, c) for r in range(14) for c in range(15) if sc2["grid"][r][c]["att"]]
    assert len(cells2) == 1
    r2, c2 = cells2[0]
    assert r2 == 2, f"篮下纵向约 2.4m 应在第 2 行，实际 {r2}"
    assert abs(c2 - 7) <= 1, f"篮下应靠近中线那一列(约第7列)，实际 {c2}"

    # 4) 合成数据的坐标范围必须落在合法球场内
    rt = synthetic_game(seed=7, duration=600.0)
    for a in rt.attempts:
        assert abs(a.x) <= COURT_WIDTH / 2 + 0.1, f"横向越界：{a.x}"
        assert abs(a.y) <= 14.0, f"纵向越界：{a.y}"
    # 三分与两分都应该有，且三分占比合理（20%~50%）
    n3 = sum(1 for a in rt.attempts if is_three_pointer(a.x, a.y)
             and not a.is_free_throw)
    n_all = sum(1 for a in rt.attempts if not a.is_free_throw)
    share = n3 / n_all
    assert 0.20 <= share <= 0.50, f"合成数据三分占比应合理，实际 {share:.1%}"


# --------------------------------------------------------------------------
# 2) 命中事件融合
# --------------------------------------------------------------------------
def test_fuse_outcome():
    # 球穿筐 + 记分牌都判命中 -> 命中且置信度高
    made, conf, src = fuse_outcome([
        Evidence("ball_through_rim", True, 1.0, 0.95),
        Evidence("scoreboard_ocr", True, 2.0, 0.9),
    ])
    assert made is True and conf > 0.9

    # 球轨迹说没进、记分牌说进了 -> 加权后仍判命中（记分牌权重 0.8，
    # 球 1.0*0.95 vs 牌 0.8*0.9 -> 球票更多，判未中）
    made, conf, src = fuse_outcome([
        Evidence("ball_through_rim", False, 1.0, 0.95),
        Evidence("scoreboard_ocr", True, 2.0, 0.9),
    ])
    assert made is False and src == "ball_through_rim"

    # 无证据 -> 未知、置信度 0
    made, conf, src = fuse_outcome([])
    assert made is None and conf == 0.0


def test_detect_rim_events():
    from aihoop.sources import _simulate_ball_flight
    import random
    rng = random.Random(1)
    made_track = _simulate_ball_flight(rng, 10.0, 5.0, 2.0, made=True)
    hits = detect_rim_events(made_track)
    assert any(h.through for h in hits), "命中球的轨迹里应能检测到穿筐"
    rng = random.Random(1)
    miss_track = _simulate_ball_flight(rng, 10.0, 5.0, 2.0, made=False)
    hits2 = detect_rim_events(miss_track)
    assert not any(h.through for h in hits2), "未中球不应被判穿筐"


# --------------------------------------------------------------------------
# 3) 球场标定
# --------------------------------------------------------------------------
def test_homography_roundtrip():
    # 构造一个已知的单应（缩放+平移），再解回来
    src = [(0, 0), (100, 0), (100, 200), (0, 200)]
    dst = [(0.0, 0.0), (15.0, 0.0), (15.0, 28.0), (0.0, 28.0)]
    H = find_homography(src, dst)
    x, y = apply_homography(H, 50, 100)
    assert abs(x - 7.5) < 1e-6 and abs(y - 14.0) < 1e-6

    # 逆变换
    Hi = invert(H)
    px, py = apply_homography(Hi, 7.5, 14.0)
    assert abs(px - 50) < 1e-6 and abs(py - 100) < 1e-6


def test_calibrate_from_corners():
    cal = calibrate_from_corners([(0, 0), (1920, 0), (1920, 1080), (0, 1080)],
                                 half_court=True)
    assert cal.reproj_error_m < 1e-6
    # 画面中心应映射到半场中心附近
    x, y = apply_homography(cal.H, 960, 540)
    assert abs(x) < 0.01 and abs(y + 7.0) < 0.01


def _value_hist(shots) -> dict:
    """出手分值分布，断言失败时用来看清到底缺哪一类。"""
    h: dict = {}
    for s in shots:
        h[s.value] = h.get(s.value, 0) + 1
    return h


# --------------------------------------------------------------------------
# 4) 端到端管线（合成数据）
# --------------------------------------------------------------------------
def _run(tmp_path: Path):
    from aihoop.pipeline import PipelineConfig, run_pipeline
    # 用 20 分钟（1200s）保证样本量足够覆盖到三分与罚球，
    # 免得测试因为随机分布偶发缺少某一类出手而假失败。
    rt = synthetic_game(seed=42, duration=1200.0)
    cfg = PipelineConfig(out_dir=str(tmp_path / "out"), make_highlights=False)
    return rt, run_pipeline(rt, cfg)


def test_pipeline_end_to_end(tmp_path):
    rt, res = _run(tmp_path)
    g = res.game

    # --- 验收 1：自动计分（含 2/3 分）---
    assert len(res.shots) > 60, "一场 20 分钟应有足够多的出手"
    kinds = {s.value for s in res.shots}
    assert 3 in kinds, f"应有三分球，实际分值分布：{_value_hist(res.shots)}"
    assert 2 in kinds, f"应有两分球，实际分值分布：{_value_hist(res.shots)}"
    assert 1 in kinds, f"应有罚球，实际分值分布：{_value_hist(res.shots)}"
    assert g["score"]["home"] > 0 and g["score"]["away"] > 0

    # 比分自洽：分节比分之和 == 总分
    assert sum(q["home"] for q in g["quarter_scores"]) == g["score"]["home"]
    assert sum(q["away"] for q in g["quarter_scores"]) == g["score"]["away"]

    # 得分 == 命中球的分值之和
    assert g["score"]["home"] + g["score"]["away"] == sum(s.points for s in res.shots)

    # --- 验收 2：球员统计 ---
    assert res.players
    assert sum(p["points"] for p in res.players) == \
        g["score"]["home"] + g["score"]["away"], "球员得分之和应等于总分"

    # --- 验收 3：命中率 ---
    hs = g["teams"]["home"]["stats"]
    assert 0.0 <= hs["fg_pct"] <= 1.0
    assert hs["fga"] >= hs["fgm"]

    # --- 验收 4：投篮热区 ---
    sc = res.shotchart
    assert sc["points"] and sc["zones"]
    assert sum(z["att"] for z in sc["zones"]) == len(res.shots)
    assert sum(z["made"] for z in sc["zones"]) == sum(1 for s in res.shots if s.made)
    assert len(sc["grid"]) > 0

    # --- 验收 5：高光（未开 ffmpeg 时也有时间码）---
    from aihoop.highlight import rank_shots
    picks = rank_shots(res.shots, 10)
    assert len(picks) == 10
    assert picks[0].points >= 0

    # --- 验收 6：战报与导出 ---
    assert "最终比分" in res.report_md
    for f in ("game.json", "players.json", "shotchart.json", "report.md",
              "report.json", "events.jsonl", "stats.csv", "shots.csv"):
        p = Path(res.out_dir) / f
        assert p.exists() and p.stat().st_size > 0, f"缺少产物 {f}"

    # 产物能被解析回来
    game_back = json.loads((Path(res.out_dir) / "game.json").read_text("utf-8"))
    assert game_back["score"] == g["score"]


def test_overrides_are_applied(tmp_path):
    """人工复核改判后，比分必须随之变化 —— 这是「可干预」的关键验证。"""
    from aihoop.pipeline import PipelineConfig, run_pipeline
    rt = synthetic_game(seed=11, duration=300.0)
    base = run_pipeline(rt, PipelineConfig(out_dir=str(tmp_path / "a"),
                                           make_highlights=False))
    # 找一个「未中」的出手，把它改判成命中三分
    idx = next(i for i, s in enumerate(sorted(base.shots, key=lambda x: x.t))
               if not s.made and s.value == 2)
    fixed = run_pipeline(rt, PipelineConfig(
        out_dir=str(tmp_path / "b"), make_highlights=False,
        overrides={idx: {"made": True, "value": 3}}))
    assert fixed.game["score"]["home"] + fixed.game["score"]["away"] > \
        base.game["score"]["home"] + base.game["score"]["away"]
    s = sorted(fixed.shots, key=lambda x: x.t)[idx]
    assert s.made is True and s.value == 3
    assert "manual" in s.tags
    assert fixed.game["teams"]["home"]["stats"]["tpm"] >= 0


def test_possessions_and_progression():
    shots = [
        Shot(t=1, team="home", player_id="H1", x=1.0, y=0.0, value=2, made=True),
        Shot(t=3, team="home", player_id="H1", x=2.0, y=1.0, value=2, made=False),
        Shot(t=30, team="away", player_id="A1", x=6.0, y=2.0, value=3, made=True),
    ]
    poss = possessions(shots, gap=6.0)
    assert len(poss) == 2
    prog = score_progression(shots)
    assert prog[0] == {"t": 0.0, "home": 0, "away": 0}
    assert prog[-1]["away"] == 3
    qs = quarter_scores(shots, periods=1)
    assert qs[0]["home"] == 2 and qs[0]["away"] == 3


def test_shots_csv_export(tmp_path):
    from aihoop.export import write_shots_csv, write_stats_csv
    shots = [Shot(t=1.5, team="home", player_id="H1", x=5.0, y=1.0,
                  value=3, made=True)]
    p = tmp_path / "s.csv"
    write_shots_csv(str(p), shots)
    assert "zone" in p.read_text(encoding="utf-8-sig").splitlines()[0]
    p2 = tmp_path / "t.csv"
    write_stats_csv(str(p2), compute_player_stats(shots), None, None)
    assert p2.exists()


# --------------------------------------------------------------------------
# 让脚本可以不用 pytest 直接运行
# --------------------------------------------------------------------------
def _run_all() -> int:
    import inspect
    import shutil
    import tempfile
    import traceback

    fns = [(n, f) for n, f in sorted(globals().items())
           if n.startswith("test_") and callable(f)]

    # 注意：不用 tempfile.TemporaryDirectory —— 在没有 Temp 目录删除权限的
    # 受限沙箱里它的清理会抛 PermissionError，把真实失败掩盖掉。
    base = ROOT / "out" / "_selftest"
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




def test_turns_ignore_small_jitter():
    """球到篮筐附近会抖出十几个小方向变化，不应算成真实的转向。"""
    from aihoop.ball import _turns
    vals = [0, 12, 24, 36, 48, 60, 72, 83, 90, 86, 92, 89, 95, 92, 98, 120, 160]
    assert _turns(vals) >= 6              # 旧口径：小抖动全算
    assert _turns(vals, 4.0) <= 3         # 新口径：忽略小抖动


def test_hoop_track_rejects_ball_lock_outlier():
    """球穿过篮筐时会被误检成篮圈；单点离群值不能影响进球判定。"""
    from aihoop.hoop import HoopTrack, Hoop
    good = lambda t, cx: (t, Hoop(cx=cx, cy=100.0, rx=30.0, ry=10.0))
    samples = [good(0.2 * i, 100.0 + i * 0.2) for i in range(11)]
    # t=1.2 处：球被当成「篮圈」，中心偏左 30px、半宽缩成 12px
    samples[6] = (1.1, Hoop(cx=70.0, cy=100.0, rx=12.0, ry=10.0))
    ht = HoopTrack(samples=samples, smooth_window=2)
    h = ht.at(1.1)
    assert abs(h.cx - 100.0) < 5.0
    assert h.rx > 25.0
    raw = HoopTrack(samples=samples, smooth_window=0)
    assert raw.at(1.1).cx == 70.0


def test_detect_shots_survives_bad_hoop_sample():
    """一次「球穿过篮筐」的轨迹，即使有一帧篮筐被球带偏，也要判成进球。

    几何说明（2026-09-27 外部复核改）：原来的合成轨迹**没有上升段**，只在"判成进球"
    时才被保留，所以把球当质点（rim_inner=0.85）时它才通过；而且轨迹的交叉点横向偏了
    20px = 0.67×rx —— 按"球半径 ≈0.53×rx"的净空判据，那是**贴筐掠过**不是干净穿筐。
    现在改成一条真正合理的弧线（升到筐上方 24px → 下落穿过筐面），交叉点横向偏移
    3.4px = 0.11×rx；被带偏的那一帧放在**穿筐之前的下落段**（t=1.0s），这样第二条断言
    （不做中值滤波就判不出来）才是真的咬得住，而不是"没有产出任何事件"的空转。
    """
    from aihoop.ball import BallCandidate
    from aihoop.hoop import HoopTrack, Hoop, HoopConfig, detect_shots
    samples = []
    for i in range(17):
        t = 0.2 + 0.1 * i
        samples.append((round(t, 2), Hoop(cx=100.0, cy=100.0, rx=30.0,
                                          ry=10.0)))
    # 球穿过筐时橙色检测会把「球」当成「篮圈」：这一帧的筐心偏到 70、半宽缩到 12
    samples[8] = (1.0, Hoop(cx=70.0, cy=100.0, rx=12.0, ry=10.0))
    ball = [
        (0.2, 145.0, 200.0), (0.3, 138.0, 172.0), (0.4, 131.0, 146.0),
        (0.5, 124.0, 124.0), (0.6, 117.0, 106.0), (0.7, 111.0, 92.0),
        (0.8, 107.0, 82.0), (0.9, 105.0, 76.0),          # 顶点（筐上方 24px）
        (1.0, 104.0, 82.0), (1.1, 103.5, 96.0),          # 筐上方 → 穿越前最后一点
        (1.2, 103.0, 112.0), (1.3, 102.0, 132.0),        # 筐下方 → 穿越后第一点
        (1.4, 101.5, 156.0), (1.5, 101.0, 184.0), (1.6, 100.5, 216.0),
        (1.7, 100.0, 250.0), (1.8, 99.5, 288.0),
    ]
    track = [BallCandidate(t=t, x=x, y=y, score=0.8, source="color")
             for (t, x, y) in ball]
    ht = HoopTrack(samples=samples, smooth_window=2)
    shots = detect_shots([track], ht, HoopConfig())
    assert len(shots) == 1
    assert shots[0].made is True
    # 不做中值滤波时，被球带偏的篮筐会使这次投篮无法判进
    # （带偏的下落段样本影响选取的穿筐区间及横向偏移，
    #   超过净空门槛 0.467×rx = 14px）
    raw = HoopTrack(samples=samples, smooth_window=0)
    raw_shots = detect_shots([track], raw, HoopConfig())
    assert raw_shots, "事件本身仍应产出（只是结果未知/不中）"
    assert all(not sh.made for sh in raw_shots)

def test_locate_shooter_uses_ball_launch_point():
    """球员归属要用「球迹起点」，不能拿抛物线中段的球位去比球员中心。"""
    from types import SimpleNamespace
    from aihoop.ball import BallCandidate
    from aihoop.sources import VideoSource
    shot = SimpleNamespace(t=1.5, release_x=1500.0, release_y=300.0)
    boxes = {
        "A": [(i * 0.2, 700, 200, 800, 500) for i in range(11)],
        "B": [(i * 0.2, 1400, 250, 1500, 600) for i in range(11)],
    }
    track = [
        BallCandidate(t=0.6, x=760, y=185, score=0.8, source="color"),
        BallCandidate(t=0.8, x=720, y=120, score=0.8, source="color"),
        BallCandidate(t=1.0, x=650, y=60, score=0.8, source="color"),
        BallCandidate(t=1.2, x=560, y=40, score=0.8, source="color"),
        BallCandidate(t=1.4, x=470, y=70, score=0.8, source="color"),
        BallCandidate(t=1.6, x=400, y=130, score=0.8, source="color"),
    ]
    pid, bbox = VideoSource._locate_shooter(None, shot, boxes, [track])
    assert pid == "A"          # 球从 A 头顶出手
    assert bbox[1] <= 200      # 用的是出手那一帧的球员框
    # 旧口径：拿 shot.release_* (1500, 300) 比球员中心会算到远处的 B
    assert abs(shot.release_x - 1450) < abs(shot.release_x - 750)

def test_jump_shooter_picks_jumping_player():
    """出手时起跳的是投篮者；另一位球员不跳，不能被算成投篮者。"""
    from types import SimpleNamespace
    from aihoop.sources import VideoSource
    times = [round(0.6 + 0.1 * i, 2) for i in range(14)]

    def ay(t):
        return 160.0 if 1.0 <= t <= 1.4 else 200.0

    boxes = {
        "A": [(t, 500.0, ay(t), 560.0, 380.0) for t in times],
        "B": [(t, 100.0, 260.0, 150.0, 430.0) for t in times],
    }
    shot = SimpleNamespace(t=1.5)
    assert VideoSource._jump_shooter(None, shot, boxes) == "A"
    # 没人起跳时返回 None，交给球迹兜底
    flat = {k: [(t, x1, 200.0, x2, y2) for (t, x1, _y, x2, y2) in v]
            for k, v in boxes.items()}
    assert VideoSource._jump_shooter(None, shot, flat) is None


if __name__ == "__main__":
    raise SystemExit(_run_all())
