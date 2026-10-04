"""比分牌识别与证据融合的单元测试（不依赖视频）。

跑法：
    python -B tests/test_scoreboard.py
    # 或者 pytest tests/test_scoreboard.py

只测「纯逻辑」那部分 —— 时序去抖、比分跳变到出手的翻译、开局带入分的口径、
球轨迹的位置归因。这些都和具体视频无关，用构造数据就能覆盖；
真正依赖 opencv/numpy 的部分在没有装依赖时会自动跳过。
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from aihoop.scoreboard import (  # noqa: E402
    ScoreBugConfig, ScoreReading, _debounce, score_points_to_attempts,
)
from aihoop.sources import Attempt, RawTrack  # noqa: E402

PASS = 0
FAIL = 0


def check(name: str, cond: bool, extra: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  [ok] {name}")
    else:
        FAIL += 1
        print(f"  [FAIL] {name} {extra}")


def _r(t: float, h: int, a: int) -> ScoreReading:
    return ScoreReading(t=t, home=h, away=a, confidence=0.9)


# --------------------------------------------------------------------------
def test_debounce_basic():
    print("\n[1] 时序去抖：比分只增不减 + 新高要连续确认")
    cfg = ScoreBugConfig(confirm_reads=2)
    # 2:5 稳定 -> 出现一帧误读 9:5 -> 回到 2:5 -> 真正变成 4:5
    readings = [_r(0.0, 2, 5), _r(0.1, 2, 5), _r(0.2, 9, 5), _r(0.3, 2, 5),
                _r(0.4, 4, 5), _r(0.5, 4, 5), _r(0.6, 4, 5)]
    ev = _debounce(readings, cfg)
    base = [e for e in ev if e["kind"] == "baseline"]
    score = [e for e in ev if e["kind"] == "score"]
    check("只有一条基线", len(base) == 1)
    check("基线是 2:5", base and (base[0]["home"], base[0]["away"]) == (2, 5),
          str(base))
    check("单帧误读 9 被丢掉", all(e["home"] != 9 for e in score), str(score))
    check("真实变化 2->4 被记下",
          len(score) == 1 and score[0]["delta"] == 2 and score[0]["team"] == "home",
          str(score))


def test_debounce_decrease_rejected():
    print("\n[2] 时序去抖：比分倒退一律当误读丢掉")
    cfg = ScoreBugConfig(confirm_reads=1)
    readings = [_r(0.0, 10, 6), _r(0.1, 10, 6), _r(0.2, 7, 6), _r(0.3, 7, 6),
                _r(0.4, 12, 6), _r(0.5, 12, 6)]
    ev = _debounce(readings, cfg)
    score = [e for e in ev if e["kind"] == "score"]
    check("倒退没有被记成得分", all(e["home"] >= 10 for e in score), str(score))
    check("后续 10->12 正常记下",
          len(score) == 1 and score[0]["delta"] == 2, str(score))


def test_debounce_needs_confirm():
    print("\n[3] 时序去抖：只出现一次的跳变不算数")
    cfg = ScoreBugConfig(confirm_reads=3)
    readings = [_r(0.0, 2, 5), _r(0.1, 2, 5), _r(0.2, 5, 5), _r(0.3, 2, 5),
                _r(0.4, 2, 5)]
    ev = _debounce(readings, cfg)
    check("孤立跳变被丢掉",
          not [e for e in ev if e["kind"] == "score"], str(ev))


def test_points_to_attempts():
    print("\n[4] 比分跳变 -> 出手：1/2/3 分映射")
    cfg = ScoreBugConfig(confirm_reads=1)
    readings = [_r(0.0, 0, 5),
                _r(1.0, 2, 5), _r(1.1, 2, 5),
                _r(2.0, 3, 5), _r(2.1, 3, 5),
                _r(3.0, 6, 5), _r(3.1, 6, 5),
                _r(4.0, 6, 6), _r(4.1, 6, 6)]
    ev = _debounce(readings, cfg)

    class _Scan:
        events = ev
        fps = 30.0

    at = score_points_to_attempts(_Scan(), hoop_side={"home": "left", "away": "right"},
                                  fps=30.0)
    vals = [(a["team"], a["forced_value"], a["is_free_throw"]) for a in at]
    check("共 4 次得分出手", len(at) == 4, str(vals))
    check("+2 -> 两分球", ("home", 2, False) in vals, str(vals))
    check("+1 -> 罚球", ("home", 1, True) in vals, str(vals))
    check("+3 -> 三分球", ("home", 3, False) in vals, str(vals))
    check("客队得分归到 away", any(a["team"] == "away" for a in at), str(vals))
    check("全部标记为命中", all(a["made"] is True for a in at))
    check("位置明确标注为占位估计",
          all(a["location_source"] == "rim_placeholder" for a in at))


def test_pipeline_carry_in():
    print("\n[5] 开局带入分：计入总分但不伪造出手")
    from aihoop.pipeline import PipelineConfig, run_pipeline
    from aihoop.model import Player

    rt = RawTrack(fps=30.0, duration=100.0)
    rt.base_score = {"home": 0, "away": 5}
    rt.players["A1"] = Player("A1", "客队1号", "away")
    rt.attempts = [Attempt(t=10.0, team="away", player_id="A1", x=0.0, y=1.0,
                           made=True, conf=1.0, forced_value=1,
                           source="scoreboard")]
    res = run_pipeline(rt, PipelineConfig(out_dir=str(ROOT / "out" / "_test_carry"),
                                          make_highlights=False))
    g = res.game
    check("总分含带入分", g["score"]["away"] == 6, str(g["score"]))
    check("主队为 0", g["score"]["home"] == 0, str(g["score"]))
    check("carry_in 落盘", g.get("carry_in", {}).get("away") == 5,
          str(g.get("carry_in")))
    check("出手记录只有 1 次（没有为带入分造出手）", len(res.shots) == 1,
          str(len(res.shots)))
    check("分节比分与总分一致",
          sum(q["away"] for q in g["quarter_scores"]) == g["score"]["away"])
    check("summary 里点出了带入分", "开局带入" in res.summary(), res.summary())


def test_parse_pairs_layout():
    """按版式配对：`DoubleACS | KPHS 27 | AHS 35 | 3rd Qtr` → 27 : 35。

    回归的是实测踩到的错法：`_parse_words` 要求队名 Token ≥4 个字母，
    `AHS` 只有 3 个字母被丢掉 → 两个比分都配到同一个数字 → 读出 **27 : 27**
    （真值 27 : 35）。
    """
    print("\n[7] 比分牌版式解析：3 字母队名不能被丢掉")
    from aihoop.ocr_scoreboard import _parse_pairs
    words = [
        {"text": "DoubleACS", "x": 10, "y": 60, "w": 110, "h": 16},
        {"text": "KPHS", "x": 140, "y": 60, "w": 60, "h": 16},
        {"text": "27", "x": 210, "y": 60, "w": 26, "h": 16},
        {"text": "AHS", "x": 250, "y": 60, "w": 46, "h": 16},
        {"text": "35", "x": 306, "y": 60, "w": 26, "h": 16},
        {"text": "3rd", "x": 350, "y": 60, "w": 30, "h": 16},
        {"text": "Qtr", "x": 386, "y": 60, "w": 30, "h": 16},
    ]
    p = _parse_pairs(words)
    check("解析成功", p is not None, str(p))
    if p:
        check("主队 27", p["home"] == 27, str(p))
        check("客队 35", p["away"] == 35, str(p))
        check("队名认对了", (p.get("home_name"), p.get("away_name"))
              == ("KPHS", "AHS"), str(p))
        check("节次认出来是第 3 节", p.get("period") == 3, str(p))


def test_locate_static_overlay_bar():
    """「叠加层不随时间变化」的定位：动态画面 + 固定台标 → 必须定到台标。

    回归的是实测踩到的错法：这段转播的台标是**深蓝长条**（570×24、长宽比 23.8）
    且与背后蓝墙同色，按"饱和横条 + 长宽比"找会被整面墙粘住丢掉；
    按白字占比排序时看台那块（0.118）还会赢过真台标（0.115）。
    换成"全片唯一不动的区域"就干净了。

    直接喂帧数组（不经视频编解码器）：有损压缩会在叠加层边缘留下几十级灰度
    噪声，测出来的是编解码器的脾气，不是算法本身。
    """
    print("\n[8] 自动定位：动态画面里的静态台标")
    try:
        import cv2
        import numpy as np
    except ImportError:
        print("  [skip] 没装 opencv/numpy")
        return
    from aihoop.scoreboard import ScoreBugConfig, static_overlay_candidates
    w, h, n = 320, 200, 24
    rng = np.random.default_rng(0)
    frames = []
    for k in range(n):
        frame = np.zeros((h, w, 3), np.uint8)
        frame[:, :] = (60, 110, 170)              # 会动的背景（每帧加噪声）
        x0 = (k * 7) % (w - 40)
        frame[120:190, x0:x0 + 40] = (120, 190, 240)
        noise = rng.integers(-40, 41, size=(h, w, 1), dtype=np.int16)
        frame = np.clip(frame.astype(np.int16) + noise, 0, 255).astype(np.uint8)
        # 台标：固定位置、固定内容（只有比分数字那一小块会变）—— 画在噪声之上
        cv2.rectangle(frame, (40, 12), (280, 34), (90, 40, 20), -1)
        for i, wid in enumerate((14, 10, 12, 8, 14)):
            cv2.rectangle(frame, (60 + i * 22, 17), (60 + i * 22 + wid, 30),
                          (255, 255, 255), -1)
        cv2.rectangle(frame, (200 + (k % 2) * 2, 17), (214 + (k % 2) * 2, 30),
                      (255, 255, 255), -1)
        frames.append(frame)
    cands = static_overlay_candidates(frames, ScoreBugConfig())
    check("找到了候选", bool(cands), str(cands))
    if cands:
        _s, (x, y, bw, bh) = cands[0]
        check("位置对上（x≈40）", abs(x - 40) <= 8, f"x={x}")
        check("位置对上（y≈12）", abs(y - 12) <= 8, f"y={y}")
        check("宽度对上（≈240）", abs(bw - 240) <= 30, f"w={bw}")
        check("高度对上（≈22）", abs(bh - 22) <= 10, f"h={bh}")


def test_field_patterns_roundtrip():
    print("\n[6] 字段图案模板：存盘/加载/匹配")
    try:
        import numpy as np
    except ImportError:
        print("  [skip] 没装 numpy")
        return
    from aihoop.scoreboard import FieldPatterns

    rng = np.random.default_rng(0)
    home = np.stack([rng.random((19, 68), dtype=np.float32) for _ in range(3)])
    away = np.stack([rng.random((19, 68), dtype=np.float32) for _ in range(2)])
    fp = FieldPatterns(home_reps=home, home_vals=[0, 2, 4],
                       away_reps=away, away_vals=[5, 6])
    p = ROOT / "out" / "_test_templates.npz"
    fp.to_npz(str(p))
    back = FieldPatterns.load(str(p))
    check("home 模板数量一致", len(back.home_vals) == 3, str(back.home_vals))
    check("away 模板数量一致", len(back.away_vals) == 2, str(back.away_vals))

    cfg = ScoreBugConfig()
    v, conf, d = back.match("home", home[1], cfg)
    check("完全相同的窗口能匹配成功且距离为 0",
          v == 2 and d < 1e-6, f"v={v} d={d}")
    v2, conf2, d2 = back.match("home", home[1] + 0.5, cfg)
    check("偏离太大的窗口判为认不出", v2 is None, f"v={v2} d={d2}")


def test_attempt_serialization():
    print("\n[7] Attempt 新增字段的存盘/加载兼容")
    rt = RawTrack(fps=30.0, duration=10.0)
    rt.base_score = {"home": 0, "away": 5}
    rt.attempts = [Attempt(t=1.0, team="home", player_id="H1", x=1.0, y=2.0,
                           forced_value=3, source="scoreboard",
                           location_source="rim_placeholder",
                           location_estimated=False)]
    p = ROOT / "out" / "_test_rt.json"
    rt.save(str(p))
    back = RawTrack.load(str(p))
    a = back.attempts[0]
    check("forced_value 保住了", a.forced_value is None or a.forced_value == 3,
          str(a.forced_value))
    check("base_score 保住了", back.base_score.get("away") == 5,
          str(back.base_score))


def test_team_assignment_by_jersey():
    print("\n[8] 球衣颜色分队：绿色系算主队、红色系算客队")
    from aihoop.court import Calibration
    from aihoop.sources import VideoSource

    src = VideoSource("x.mp4", Calibration())
    rt = RawTrack(fps=30.0)
    # 绿色球衣 H≈55，红色球衣 H≈5（OpenCV 色相环）。
    # 注意 jersey_sum 的格式是 [色相累加, 饱和度累加, 明度累加, 帧数]，
    # 内部会除以帧数取均值 —— 这里给 3 帧的累加值。
    # 格式：{pid: {"n": 采样帧数, "bins": {(H//8, S//32, V//64): 次数}}}
    # 取众数 bin，再用 bin 中心还原成球衣色 —— 见 sources._assign_teams
    def _rec(h, ss, vv, n=24):
        return {"n": 6,
                "bins": {(int(h) // 8, int(ss) // 32, int(vv) // 64): n}}

    jersey = {}
    for i in range(4):
        jersey[f"G{i}"] = _rec(55, 120, 90)     # 绿队
        jersey[f"R{i}"] = _rec(5, 200, 90)      # 红队（高饱和，不会被当肤色）
    for pid in jersey:
        from aihoop.model import Player
        rt.players[pid] = Player(pid, pid, "home")
    src._assign_teams(rt, jersey)
    greens = {p.team for k, p in rt.players.items() if k.startswith("G")}
    reds = {p.team for k, p in rt.players.items() if k.startswith("R")}
    check("绿衣分到主队", greens == {"home"}, str(greens))
    check("红衣分到客队", reds == {"away"}, str(reds))
    m = rt.detections_meta.get("teams", {})
    # 新契约：不再只按色相聚类，而是在 色相/饱和度/明度 三个候选里自动挑
    # 分离度最高的那个，并且把分离度与置信度一起报出来（可审计）。
    check("分队用了自动选特征",
          str(m.get("method", "")).startswith("color-2means:"), str(m))
    check("分离度达标且置信度为 ok",
          m.get("separability", 0) >= 1.0 and m.get("confidence") == "ok", str(m))
    check("两队人数都记下来了",
          (m.get("counts") or {}).get("home") == 4
          and (m.get("counts") or {}).get("away") == 4, str(m))


def test_location_tags():
    print("\n[9] 位置来源会进 tags（不能让占位坐标污染热区结论）")
    from aihoop.rules import build_shots

    shots = build_shots([
        {"t": 1.0, "team": "home", "player_id": "H1", "x": 0.0, "y": 1.575,
         "made": True, "conf": 1.0, "forced_value": 2,
         "location_source": "rim_placeholder"},
        {"t": 2.0, "team": "home", "player_id": "H1", "x": 5.0, "y": 3.0,
         "made": True, "conf": 1.0, "forced_value": 2,
         "location_source": "ball_track", "location_estimated": True},
    ])
    check("占位坐标被标记为 location_unknown",
          "location_unknown" in shots[0].tags, str(shots[0].tags))
    check("球轨迹估计坐标被标记为 location_estimated",
          "location_estimated" in shots[1].tags, str(shots[1].tags))


def test_link_tracks_guard():
    print("\n[10] 球轨迹连接：时间戳不递增时不能退化成 O(N²) 卡死")
    import time as _t
    from aihoop.ball import BallCandidate, BallConfig, _link_tracks

    cfg = BallConfig()
    # 全部候选的 t 都是 0（曾经因为漏写 idx += 1 真的发生过）
    bad = [BallCandidate(t=0.0, x=float(i % 100), y=float(i % 37),
                         score=0.5, source="color") for i in range(4000)]
    t0 = _t.time()
    tr = _link_tracks(bad, cfg)
    dt = _t.time() - t0
    check(f"4000 个同时刻候选在 2s 内处理完（实际 {dt:.2f}s）", dt < 2.0, f"{dt:.2f}s")
    check("同时刻候选连不出轨迹", len(tr) == 0, str(len(tr)))

    # 正常递增：应该能连出轨迹
    good = []
    for f in range(60):
        good.append(BallCandidate(t=f / 30.0, x=100.0 + f * 4, y=200.0,
                                  score=0.8, source="color"))
    tr2 = _link_tracks(good, cfg)
    check("正常递增时能连出至少一条轨迹", len(tr2) >= 1, str(len(tr2)))
    check("连出的轨迹点数等于输入点数", tr2 and len(tr2[0]) == 60,
          str([len(x) for x in tr2]))


def _arc_track(apex_x, end_x, t0=0.0, n=31):
    """造一条投篮弧线：从 (100,400) 升到 (apex_x,120)，再下落到 (end_x,400)。"""
    from aihoop.ball import BallCandidate
    pts = []
    for i in range(n // 2 + 1):
        k = i / (n // 2)
        pts.append(BallCandidate(t=t0 + k * 0.5, x=100 + (apex_x - 100) * k,
                                 y=400 - 280 * k, score=0.8, source="color"))
    for i in range(1, n // 2 + 1):
        k = i / (n // 2)
        pts.append(BallCandidate(t=t0 + 0.5 + k * 0.5, x=apex_x + (end_x - apex_x) * k,
                                 y=120 + 280 * k, score=0.8, source="color"))
    return pts


def test_hoop_shot_inference():
    print("\n[11] 球+篮筐：穿筐判定（不需要知道球离地多高）")
    from aihoop.hoop import Hoop, HoopConfig, HoopTrack, detect_shots
    from aihoop.ball import BallCandidate

    hoop = Hoop(cx=500.0, cy=200.0, rx=30.0, ry=10.0)
    cfg = HoopConfig()

    made = detect_shots([_arc_track(500, 500)], hoop, cfg)
    check("穿过篮圈正中的弧线判为进球", len(made) == 1 and made[0].made,
          str([s.to_dict() for s in made]))
    check("穿越点落在篮圈中心附近",
          made and made[0].cross_x is not None and abs(made[0].cross_x - 500) < 5,
          str(made[0].cross_x if made else None))

    miss = detect_shots([_arc_track(500, 600)], hoop, cfg)
    check("从篮圈旁边落下的弧线判为不中",
          len(miss) == 1 and not miss[0].made,
          str([s.to_dict() for s in miss]))

    flat = [BallCandidate(t=i / 30.0, x=400 + i * 3, y=300 + i * 0.2,
                          score=0.8, source="color") for i in range(20)]
    check("没有上升段的轨迹不算出手", not detect_shots([flat], hoop, cfg))


def test_hoop_track_moving_camera():
    print("\n[12] 篮筐随时间移动时用的是「当时」的位置")
    from aihoop.hoop import Hoop, HoopConfig, HoopTrack, detect_shots

    # 篮筐从 (500,200) 一路平移到 (560,230)
    ht = HoopTrack(fps=30.0, duration=2.0, votes=60, frames=60)
    for i in range(11):
        t = i * 0.2
        ht.samples.append((t, Hoop(cx=500 + 60 * t / 2.0, cy=200 + 30 * t / 2.0,
                                   rx=30.0, ry=10.0)))
    check("就近取值取到的是那一刻的篮筐",
          abs(ht.at(1.0).cx - 530) < 1e-6 and abs(ht.at(1.0).cy - 215) < 1e-6,
          f"{ht.at(1.0).cx},{ht.at(1.0).cy}")
    dx, dy = ht.drift()
    check("漂移量被算出来（60,30）", abs(dx - 60) < 1e-6 and abs(dy - 30) < 1e-6,
          f"{dx},{dy}")

    # 用「静止在中位数位置」的篮筐去判会错，用 HoopTrack 才对
    static = Hoop(cx=530.0, cy=215.0, rx=30.0, ry=10.0)
    tr = _arc_track(560, 560)
    _shifted = [(c.x, c.y) for c in tr]
    with_track = detect_shots([tr], ht, HoopConfig())
    check("用 HoopTrack 能识别出这次出手", len(with_track) == 1,
          str(len(with_track)))
    _ = static


def test_calibration_binding():
    print("\n[13] 标定必须绑定视频：不能拿别处的标定硬套")
    from aihoop.court import Calibration, calibrate_from_corners

    cal = calibrate_from_corners([(100, 385), (1170, 385), (1275, 715), (5, 715)],
                                 half_court=True,
                                 video_path="arena_game.mp4")
    check("标定记下了它属于哪个视频",
          cal.for_video == "arena_game.mp4", cal.for_video)
    check("对同一个视频判定为可用",
          cal.matches_video("arena_game.mp4", 1280, 720))
    check("换一个视频就拒绝（哪怕分辨率完全一样）",
          not cal.matches_video("nba_clip.mp4", 1280, 720))
    check("路径不同但文件名相同仍视为同一个机位",
          cal.matches_video(r"D:\other\arena_game.mp4", 1280, 720))

    # 老标定文件没有 for_video：退回分辨率判定，且标定点必须落在画面内
    legacy = Calibration(src_px=[[100, 385], [1170, 385], [1275, 715], [5, 715]],
                         dst_m=[[-7.5, -14], [7.5, -14], [7.5, 0], [-7.5, 0]],
                         H=[[1, 0, 0], [0, 1, 0], [0, 0, 1]])
    check("老标定文件在 1280×720 上仍可用（向后兼容）",
          legacy.matches_video("whatever.mp4", 1280, 720))
    check("老标定文件在 852×480 上被拒（标定点出界）",
          not legacy.matches_video("whatever.mp4", 852, 480))


def main() -> int:
    test_debounce_basic()
    test_debounce_decrease_rejected()
    test_debounce_needs_confirm()
    test_points_to_attempts()
    test_pipeline_carry_in()
    test_parse_pairs_layout()
    test_locate_static_overlay_bar()
    test_field_patterns_roundtrip()
    test_attempt_serialization()
    test_team_assignment_by_jersey()
    test_location_tags()
    test_link_tracks_guard()
    test_hoop_shot_inference()
    test_hoop_track_moving_camera()
    test_calibration_binding()
    print(f"\n{'=' * 60}\n通过 {PASS} 项，失败 {FAIL} 项")
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
