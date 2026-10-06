"""分析管线 —— 把 RawTrack 变成前端/报告需要的所有产物。

  raw_track.json  ──►  pipeline.run()
                         ├── game.json        总览：比分、分节、时间轴、走势
                         ├── players.json     球员统计
                         ├── shotchart.json   热区（点/分区/网格）
                         ├── events.jsonl     完整事件流
                         ├── report.md        文字战报
                         ├── report.json      结构化战报
                         ├── stats.csv        Excel 可打开的统计表
                         ├── highlights/*.mp4 高光片段
                         └── [套餐 B] tactics.json / tactics_frames.jsonl
                             / passes.csv / spacing.csv   战术层产物
"""
from __future__ import annotations

import json
import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

from .model import Event, EventType, Player, Shot, write_jsonl
from .rules import (
    RulesConfig, build_shots, compute_player_stats, compute_team_stats,
    quarter_scores, score_progression, shot_chart, possessions, zone_of,
)
from .sources import RawTrack
from .tactics import (
    TacticsConfig, build_tactics, build_tactics_frames, write_tactics_frames,
)
from .report import build_report_md, build_report_json
from .export import write_stats_csv, write_shots_csv, \
    write_passes_csv, write_spacing_csv
from .highlight import make_highlights


@dataclass
class PipelineConfig:
    out_dir: str = "out"
    rules: RulesConfig = field(default_factory=RulesConfig)
    make_highlights: bool = True
    highlight_limit: int = 15
    video_path: Optional[str] = None
    # 人工复核改判：{出手序号: {"made": bool, "value": 1|2|3}}
    # 出手序号 = 按时间排序后的下标，与 game["timeline"] 的下标一致。
    # 有改判时该次出手直接采信人工结论（confidence=1.0，source=manual）。
    overrides: dict = field(default_factory=dict)
    # 人工补录的进球/出手（自动检测漏掉时使用）
    extra_attempts: list = field(default_factory=list)
    # 套餐 B：战术层（控球/传球网络/阵型/空间/俯视战术图）。
    # 关掉它 = 退化成套餐 A 的产物集合，方便对比答辩。
    make_tactics: bool = True
    tactics: Optional[TacticsConfig] = None


@dataclass
class PipelineResult:
    out_dir: str
    game: dict
    players: list[dict]
    shotchart: dict
    shots: list[Shot]
    events: list[Event]
    report_md: str
    files: dict = field(default_factory=dict)
    tactics: Optional[dict] = None

    def summary(self) -> str:
        g = self.game
        extra = ""
        carry = g.get("carry_in") or {}
        if carry.get("home") or carry.get("away"):
            if g.get("carry_counted"):
                extra = (f" | 开局带入 {carry.get('home', 0)}:"
                         f"{carry.get('away', 0)}")
            else:
                extra = (f" | 比分牌参考 {carry.get('home', 0)}:"
                         f"{carry.get('away', 0)}（不计入本片段得分）")
        tac = ""
        if isinstance(g.get("tactics"), dict) and g["tactics"].get("available"):
            t = g["tactics"]
            tac = (f" | 回合 {t.get('possessions', 0)} 次 / 传球 "
                   f"{t.get('passes', 0)} 次 / 阵型 {t.get('formation', '-')}")
        return (f"比分 {g['teams']['home']['name']} {g['score']['home']} : "
                f"{g['score']['away']} {g['teams']['away']['name']}{extra} | "
                f"出手 {len(self.shots)} 次 | 事件 {len(self.events)} 条{tac} | "
                f"产物目录 {self.out_dir}")


def run_pipeline(rt: RawTrack, cfg: Optional[PipelineConfig] = None,
                 progress: Optional[Callable[[float, str], None]] = None
                 ) -> PipelineResult:
    cfg = cfg or PipelineConfig()
    out = Path(cfg.out_dir)
    out.mkdir(parents=True, exist_ok=True)

    def step(p: float, msg: str):
        if progress:
            progress(p, msg)

    step(0.05, "载入检测结果")
    # ---- 1) 出手 -> Shot（计分规则引擎 + 事件融合）----
    step(0.20, "计分规则引擎：分值判定 + 命中融合")
    attempts = [a.to_dict() for a in rt.attempts]
    # 人工补录的出手先并入，再走同一套规则引擎/统计/战报。
    for d in (cfg.extra_attempts or []):
        if not d:
            continue
        d = dict(d)
        d.setdefault("made", True)
        d.setdefault("conf", 1.0)
        d.setdefault("source", "manual")
        d.setdefault("location_source", "manual")
        d.setdefault("location_estimated", False)
        d.setdefault("counts_for_score", True)
        d.setdefault("value_source", "manual")
        d.setdefault("manual", True)
        d.setdefault("x", 0.0)
        d.setdefault("y", 0.0)
        attempts.append(d)
    for d in attempts:
        # made=None / conf=None 表示「没有先验证据」，不要污染融合环节
        for k in ("made", "conf"):
            if d.get(k) is None:
                d.pop(k, None)

    # 人工复核的改判：按时间排序后的下标定位，和 timeline 下标一一对应
    if cfg.overrides:
        order = sorted(range(len(attempts)), key=lambda i: attempts[i]["t"])
        for idx, fix in cfg.overrides.items():
            if 0 <= idx < len(order):
                a = attempts[order[idx]]
                if fix.get("made") is not None:
                    a["made"] = bool(fix["made"])
                    a["conf"] = 1.0
                    a["manual"] = True
                if fix.get("value") in (1, 2, 3):
                    a["is_free_throw"] = (fix["value"] == 1)
                    # 非罚球时若人工指定 2/3 分，用坐标无法表达，这里记录强制分值
                    if fix["value"] in (2, 3):
                        a["forced_value"] = fix["value"]
                    a["manual"] = True

    shots = build_shots(attempts, ball_track=rt.ball_track,
                        scoreboard_events=rt.scoreboard_events, cfg=cfg.rules)
    step(0.45, f"生成 {len(shots)} 次出手记录")

    # ---- 2) 事件流 ----
    events: list[Event] = []
    for e in rt.detections_meta.get("events", []):
        events.append(Event(t=float(e["t"]), type=e["type"],
                            team=e.get("team", ""),
                            player_id=e.get("player_id", ""),
                            period=int(e.get("period", 1))))
    events.sort(key=lambda e: e.t)

    # ---- 3) 统计 ----
    step(0.60, "计算球队/球员统计、热区")
    home = compute_team_stats(shots, "home")
    away = compute_team_stats(shots, "away")
    player_rows = compute_player_stats(shots, rt.players, events)

    for side, stats in (("home", home), ("away", away)):
        total = sum(p["points"] for p in player_rows if p["team"] == side)
        if total != stats.points:
            raise ValueError(f"队别统计不一致：{side} 球员合计 {total}，球队得分 {stats.points}")

    # ---- 4) 比分/走势 ----
    meta = _evidence_meta(rt)      # 先取证据摘要：下面的计分口径要用到比分牌读数
    prog = score_progression(shots)
    qs = quarter_scores(shots)
    # 开局带入比分（视频从半场中间开始录时比分牌上已有的分）计入总分，
    # 但**不伪造出手**：出手统计仍然只统计本片段里真实发生的那些。
    # 计分口径：court/visual 按场上检测到的进球计分，比分牌只作参考；
    # scoreboard 把比分牌带入分计入总分；auto（默认）**只要比分牌真读出来了**
    # 就用比分牌口径 —— 转播素材上这才是"正常"的结果：
    # 实测这段校园转播画面写着 KPH 27 : AHS 35，老默认口径却输出 0:0，
    # 用户看到的就是"分析结果和画面对不上"。
    policy = str(rt.detections_meta.get("score_policy", "scoreboard")
                 or "scoreboard").lower()
    carry = {"home": int((rt.base_score or {}).get("home", 0) or 0),
             "away": int((rt.base_score or {}).get("away", 0) or 0)}
    sb_meta = meta.get("scoreboard") or {}
    sb_score_events = [e for e in (sb_meta.get("events") or [])
                       if e.get("kind") == "score"]
    sb_readable = bool(sb_meta) and (bool(sb_score_events)
                                    or carry["home"] or carry["away"])
    use_carry_for_score = policy == "scoreboard" or (policy == "auto"
                                                    and sb_readable)
    # 本片段真正打进的分数
    clip_score = {"home": home.points, "away": away.points}
    if use_carry_for_score and (carry["home"] or carry["away"]):
        if qs:
            bp = int(getattr(rt, "base_period", 1) or 1)
            if not (1 <= bp <= len(qs)):
                bp = 1
            qs[bp - 1]["home"] += carry["home"]
            qs[bp - 1]["away"] += carry["away"]
    if use_carry_for_score:
        score = {"home": clip_score["home"] + carry["home"],
                 "away": clip_score["away"] + carry["away"]}
    else:
        score = dict(clip_score)
    meta["score_policy"] = policy

    # 每节比分要与总分一致 —— 这里做个自检，答辩时能讲「数据一致性校验」
    assert sum(q["home"] for q in qs) == score["home"], "分节比分与总分不一致(home)"
    assert sum(q["away"] for q in qs) == score["away"], "分节比分与总分不一致(away)"

    # ---- 标定是否适用于这段视频 ----
    # 不适用时**不再产出**热区/战术：球场坐标整片错位，画出来像真的但全是垃圾，
    # 比"没有热区"更糟（用户实测反馈：战术图和热点完全不对）。
    _cal = meta.get("calibration_for_value") or {}
    cal_usable = bool(meta.get("calibration_valid", True)) and \
        bool(_cal.get("ok", True))
    cal_reason = ""
    if not cal_usable:
        cal_reason = (_cal.get("reason") or meta.get("calibration_rejected")
                      or "这份球场标定不适用于这段视频（机位/分辨率不匹配）")

    # 用户手动标定即使没过柔性拟合分数，也可以用于展示性产物并标注原因；
    # 已标记 position_unverified、退化或篮筐独立校验失败的结果仍会被拒绝。
    cal_source = str(getattr(_cal, "method", "") or
                     meta.get("calibration_method", "") or "")
    manual_cal = bool(meta.get("calibration_is_manual")) or \
        cal_source.startswith(("web-keypoints", "auto-identify", "manual"))
    # 退化（近共线/重合）标定**不能**走"手动标定就放行"这条口子：它的重投影误差
    # 也是 0.00m，但坐标整片是错的（实测篮筐被投到 33m 外）。
    # 同理，"独立校验（画面里那个真篮筐）没过"也必须一票否决 —— 那份标定连真值点
    # 都解释不了，画出来的热区/战术图只会是错的。
    _deg = meta.get("calibration_degeneracy") or {}
    cal_degenerate = bool(_deg.get("degenerate"))
    hoop_check_bad = bool(_cal) and _cal.get("ok") is False
    if cal_degenerate:
        cal_reason = (_deg.get("reason") or cal_reason
                      or "这份球场标定的点位退化（近共线/重合），坐标不可用")
    elif hoop_check_bad and _cal.get("reason"):
        cal_reason = _cal["reason"]
    # 注意：这里**不能**写 `float(meta.get("calibration_rmse_m") or 99)` ——
    # 误差正好是 0.0 的完美标定会被 `or` 当成假值、判成 99m 而关掉热区
    # （同样的坑在 sources.py 里也踩过一次）。
    _rmse = meta.get("calibration_rmse_m")
    _rmse = 99.0 if _rmse is None else float(_rmse)
    cal_position_unverified = bool(meta.get("calibration_position_unverified"))
    cal_charts = cal_usable or (manual_cal and not cal_degenerate
                                and not hoop_check_bad and not cal_position_unverified
                                and _rmse < 1.5)

    if cal_charts:
        sc = shot_chart(shots)
        zones_by_team = {t: shot_chart(shots, team=t)["zones"]
                         for t in ("home", "away")}
    else:
        sc = {"points": [], "zones": {}, "bin_size": 0.0,
              "unavailable": True, "reason": cal_reason}
        zones_by_team = {t: {} for t in ("home", "away")}
    meta["court_outputs_available"] = cal_charts
    meta["court_outputs_unverified"] = bool(cal_charts and not cal_usable)
    if not cal_charts:
        meta["court_outputs_reason"] = cal_reason
    elif not cal_usable:
        meta["court_outputs_reason"] = (
            "热区/战术图基于**你手动标的**球场标定（%s）；"
            "自动校验没通过（%s）—— 位置可能有偏差，请对照原始画面核对。"
            % (meta.get("calibration_method") or "手动标定", cal_reason))

    game = {
        "score": score,
        "clip_score": clip_score,
        "score_policy": policy,
        "scoreboard_reference": carry,
        "teams": {
            "home": {"name": _team_name(rt, "home") or "主队",
                     "stats": home.to_dict()},
            "away": {"name": _team_name(rt, "away") or "客队",
                     "stats": away.to_dict()},
        },
        "quarter_scores": qs,
        "progression": prog,
        "timeline": [{**_shot_event(s, rt), "index": i}
                     for i, s in enumerate(sorted(shots, key=lambda x: x.t))],
        "periods": 4,
        "duration": rt.duration,
        "fps": rt.fps,
        "needs_review": [{**s.to_dict(), "index": i}
                         for i, s in enumerate(sorted(shots, key=lambda x: x.t))
                         if "needs_review" in s.tags],
        "possessions": len(possessions(shots)),
        "carry_in": carry,
        # 带入分**到底算进总分没有**（口径是 auto 时取决于比分牌是否真读出来了）。
        # 报告/摘要/前端都该看这个布尔量，而不是各自去猜 policy 的含义 ——
        # 实测踩到：auto 口径下带入分已计入总分，CLI 却仍打印"不计入本片段得分"。
        "carry_counted": bool(use_carry_for_score
                              and (carry["home"] or carry["away"])),
        "base_period": int(getattr(rt, "base_period", 1) or 1),
        "judgement": _judgement(meta, shots),
        "unmatched_goals": [s.to_dict() for s in shots
                            if s.made and not s.counts_for_score],
        "meta": meta,    }

    # ---- 4.5) 套餐 B：战术层 ----
    # 放在统计之后、战报之前：战报要引用战术结论（阵型占比 / 传球网络摘要）。
    tactics_data: Optional[dict] = None
    tactics_frames: list[dict] = []
    # 球员坐标能不能用？两条路：① 已通过校验的静态标定；② 自动逐帧标定（兜底，标注未校验）
    sliding_ok, sliding_why = _sliding_position_ok(meta)
    # ②之前先查它给的 H 本身合不合理：sliding 的分数（ratio/达标率）会骗人，
    #   实测自称 10.96/1.000，投出来却是 y 跨度 71m、x 全挤在边线上。
    if sliding_ok:
        _h_ok, _h_why = _sliding_homography_sane(meta)
        if not _h_ok:
            sliding_ok = False
            sliding_why = _h_why
    pos_via_sliding = bool(not cal_usable and sliding_ok)
    # 最后一道闸：坐标自身在物理上说得通吗？
    # 分数（ratio/达标率）会骗人（实测 sliding 自称 14.5 / 0.999，坐标却把 90% 的
    # 球员压在边线上），但坐标的分布骗不了人。过不了就整层不出，并说明原因。
    coords_ok, coords_why = _tactics_coords_sane(rt.player_track)
    if cfg.make_tactics and not coords_ok:
        step(0.68, "战术层：跳过（球员坐标在物理上说不通）")
        pos_via_sliding = False
        cal_usable = False
        sliding_why = coords_why
    if cfg.make_tactics and not cal_usable and not pos_via_sliding:
        # 战术层整层都建立在球场坐标上 —— 标定错，它就整层错。
        # 这里明确标成"不适用"，而不是给一张错的俯视图。
        step(0.68, "战术层：跳过（没有可用的球员球场坐标）")
        # 优先说"自动逐帧标定为什么不够"，其次才是静态标定的结论 ——
        # 用户真正要的信息是"我离出战术图还差什么"。
        why = sliding_why or cal_reason or "没有可用的球员球场坐标"
        game["tactics"] = {
            "available": False, "reason": why,
            "player_track_samples": int((meta.get("player_track") or {}).get("samples") or 0),
            "note": "战术分析（控球/传球网络/阵型/空间/俯视图）需要可靠的球员球场坐标；"
                    "这一场既没有通过校验的球场标定，自动逐帧标定也没达到质量门槛。"
                    "修法：在「上传与分析」页重新标一次球场（建议同一帧里点 6 个以上"
                    "不在同一条线上的特征点），或改用固定机位素材。"}
    elif cfg.make_tactics:
        step(0.68, "战术层：控球归属 / 传球网络 / 阵型 / 空间")
        tcfg = cfg.tactics or TacticsConfig()
        # 注意这里传的是**已定稿的 shots**（含人工复核改判），
        # 这样"这次回合拿了多少分"跟记分牌、战报永远是同一口径。
        tactics_data = build_tactics(
            rt.player_track, rt.ball_track, rt.players, shots,
            duration=rt.duration, cfg=tcfg)
        if tactics_data.get("available") and pos_via_sliding:
            # 位置来自自动逐帧标定：坐标**没有经过独立校验**，必须显式标注。
            # 展示性结论照给（用户要的就是能看战术图），但绝不冒充"已校验"。
            tactics_data["position_unverified"] = True
            tactics_data["position_source"] = "sliding_calibration"
            tactics_data["position_note"] = (
                "球员位置来自**自动逐帧标定**（%s）—— 这份标定是按球场线自动拟合的，"
                "没有用画面里的真值点独立校验过，所以阵型/间距/位置请当参考值，"
                "建议对照原始画面核对；要更可信请重新手工标定这个机位。"
                % (sliding_why or "逐帧拟合球场线"))
            tactics_data.setdefault("notes", []).append(
                "⚠ 位置未校验：" + tactics_data["position_note"])
        elif tactics_data.get("available"):
            tactics_data["position_unverified"] = False
            tactics_data["position_source"] = "static_calibration"
        if tactics_data.get("available"):
            tactics_frames = build_tactics_frames(
                rt.player_track, rt.ball_track, duration=rt.duration, cfg=tcfg)
        game["tactics"] = _tactics_summary(tactics_data)

    # ---- 5) 战报 ----
    step(0.72, "生成战报")
    report_json = build_report_json(game, player_rows, shots, tactics_data)
    report_md = build_report_md(game, player_rows, shots, zones_by_team,
                                tactics_data)

    # ---- 6) 导出 ----
    step(0.82, "导出 CSV / JSON")
    Path(out, "game.json").write_text(
        json.dumps(game, ensure_ascii=False, indent=2), encoding="utf-8")
    Path(out, "players.json").write_text(
        json.dumps(player_rows, ensure_ascii=False, indent=2), encoding="utf-8")
    Path(out, "shotchart.json").write_text(
        json.dumps({"all": sc, "by_team": zones_by_team},
                   ensure_ascii=False, indent=2), encoding="utf-8")
    Path(out, "report.json").write_text(
        json.dumps(report_json, ensure_ascii=False, indent=2), encoding="utf-8")
    Path(out, "report.md").write_text(report_md, encoding="utf-8")
    write_jsonl(str(Path(out, "events.jsonl")),
                [s.to_dict() for s in shots] +
                [e.to_dict() for e in events])
    write_stats_csv(str(Path(out, "stats.csv")), player_rows, home, away)
    write_shots_csv(str(Path(out, "shots.csv")), shots)

    if tactics_data is not None:
        Path(out, "tactics.json").write_text(
            json.dumps(tactics_data, ensure_ascii=False, indent=2),
            encoding="utf-8")
        write_tactics_frames(str(Path(out, "tactics_frames.jsonl")),
                             tactics_frames)
        write_passes_csv(str(Path(out, "passes.csv")), tactics_data)
        write_spacing_csv(str(Path(out, "spacing.csv")), tactics_data)

    files = {
        "game": "game.json", "players": "players.json",
        "shotchart": "shotchart.json", "events": "events.jsonl",
        "report_md": "report.md", "report_json": "report.json",
        "stats_csv": "stats.csv", "shots_csv": "shots.csv",
    }
    if tactics_data is not None:
        files["tactics"] = "tactics.json"
        files["tactics_frames"] = "tactics_frames.jsonl"
        files["passes_csv"] = "passes.csv"
        files["spacing_csv"] = "spacing.csv"

    # ---- 7) 高光 ----
    clips: list[dict] = []
    if cfg.make_highlights:
        step(0.88, "生成高光片段")
        video = cfg.video_path or rt.video_path
        clips = make_highlights(shots, video, str(out / "highlights"),
                                limit=cfg.highlight_limit, cfg=cfg.rules,
                                duration=rt.duration)
        files["clips"] = "highlights/index.json"
    game["highlights"] = clips
    Path(out, "game.json").write_text(
        json.dumps(game, ensure_ascii=False, indent=2), encoding="utf-8")

    step(1.0, "完成")
    return PipelineResult(out_dir=str(out), game=game, players=player_rows,
                          shotchart=sc, shots=shots, events=events,
                          report_md=report_md, files=files,
                          tactics=tactics_data)


def _tactics_summary(t: Optional[dict]) -> dict:
    """把战术结论压成一个小摘要塞进 game.json。

    完整数据在 tactics.json（几百 KB），而 game.json 是每个页面都会拉的，
    所以这里只放"总览页/侧边栏一眼能看完"的几个数，再加上一个 available 标记 ——
    战术页据此决定是去拉 tactics.json 还是直接提示"本场没有球员轨迹"。
    """
    if not t:
        return {"available": False, "reason": "战术层未启用"}
    if not t.get("available"):
        return {"available": False, "reason": t.get("reason", "")}
    poss = t.get("possession") or {}
    passes = t.get("passes") or {}
    form = t.get("formation") or {}
    summary = {}
    for team, d in (form.get("summary") or {}).items():
        off = (d.get("offense") or [{}])[0].get("label", "-")
        deff = (d.get("defense") or [{}])[0].get("label", "-")
        summary[team] = {"offense": off, "defense": deff}
    return {
        "available": True,
        "possessions": poss.get("total", 0),
        "avg_passes": poss.get("avg_passes", 0),
        "passes": passes.get("total", 0),
        "turnovers": passes.get("turnovers", 0),
        "attack_side": t.get("attack_side", ""),
        "formation": summary,
        "spacing": (t.get("spacing") or {}).get("teams", {}),
        "coverage": t.get("coverage", {}),
        # 位置来源：static_calibration（已校验）/ sliding_calibration（自动逐帧，未校验）
        "position_source": t.get("position_source", ""),
        "position_unverified": bool(t.get("position_unverified")),
        "position_note": t.get("position_note", ""),
    }


def _evidence_meta(rt: RawTrack) -> dict:
    """把「这次判定用了哪几路证据、各自的质量如何」写进 game.json。

    答辩时最有用的信息：比分是哪来的、球追到了多少、分队是怎么分的。
    这里只保留摘要，完整读数在 raw_track.json 里。
    """
    d = rt.detections_meta or {}
    meta = {"source": d.get("source", "unknown"),
            "fps": rt.fps, "duration": rt.duration}
    sb = d.get("scoreboard")
    if sb:
        meta["scoreboard"] = {
            # source：这份比分是**怎么读出来的**（template / ocr / manual-ocr）。
            # 报告与界面要显示它 —— 用户最关心的第一件事就是"比分哪来的、可不可信"。
            "source": sb.get("source"),
            "frames_hit": sb.get("frames_hit"), "frames_read": sb.get("frames_read"),
            "bug": sb.get("bug"),
            "events": [e for e in sb.get("events", []) if e.get("kind") == "score"],
        }
    if d.get("scoreboard_bar"):
        meta["scoreboard_bar"] = d["scoreboard_bar"]
    if d.get("shot_engine"):
        meta["shot_engine"] = d["shot_engine"]
    if d.get("legacy_shots"):
        legacy = d["legacy_shots"]
        # shot_candidates = **可能漏检的出手**：只给界面提示与回看时间，
        # 不进 timeline、不进出手次数与命中统计（判定仍只由 events 决定）。
        meta["shot_engine_details"] = {k: legacy.get(k) for k in
            ("version", "config", "model_path", "model_sha256", "real_ball_frames", "rim_frames", "trace_schema", "rim_tracking", "shot_candidates")}
    if d.get("visual"):
        meta["visual"] = d["visual"]
    if d.get("hoop"):
        meta["hoop"] = {k: v for k, v in d["hoop"].items() if k != "samples"}
    if "calibration_valid" in d:
        meta["calibration_valid"] = d["calibration_valid"]
    if "calibration_position_unverified" in d:
        meta["calibration_position_unverified"] = bool(
            d["calibration_position_unverified"])
    # 标定的来源与误差：判断"是不是用户手动标的"（手动标定即使自动校验没过，
    # 也允许出热区/战术图，并显式标注未校验 —— 见下面的 court_outputs_* 逻辑）
    meta["calibration_method"] = d.get("calibration_method", "")
    meta["calibration_rmse_m"] = d.get("calibration_rmse_m", 99.0)
    if d.get("visual_error"):
        meta["visual_error"] = d["visual_error"]
    if d.get("final_score"):
        meta["scoreboard_final"] = d["final_score"]
    if d.get("ball"):
        meta["ball"] = d["ball"]
    if d.get("attempts"):
        meta["attempts"] = d["attempts"]
    if d.get("teams"):
        meta["teams"] = d["teams"]
    if d.get("scoreboard_error"):
        meta["scoreboard_error"] = d["scoreboard_error"]
    # ⚠️ `ocr_scoreboard_error` 是**另一条**错误（OCR 路径抛异常），以前只转发
    # `scoreboard_error`（模板路径），于是"比分牌自动定位失败"这件事**从来没到达
    # 界面** —— 用户看到的就是"比分一直是 0:0，按计分板判进球的功能没了"
    # （实测原话）。这里连同 sources 给的行动建议一起转发。
    if d.get("ocr_scoreboard_error"):
        meta["scoreboard_error"] = (
            meta.get("scoreboard_error") or d["ocr_scoreboard_error"]
            or d.get("ocr_scoreboard_error"))
        meta["ocr_scoreboard_error"] = d["ocr_scoreboard_error"]
    if d.get("scoreboard_action"):
        meta["scoreboard_action"] = d["scoreboard_action"]
    if d.get("scoreboard_absent"):
        meta["scoreboard_absent"] = True
    # ---- 球员球场坐标的来源与质量（战术层门槛要用，前端也要显示）----
    # 静态标定不可用时，sources 会自动改用**逐帧滑动标定**把球员投成球场坐标
    # （player_track_source == "sliding_calibration"）。它没有经过独立校验，
    # 所以要把"来源 + 质量读数"一起带出来，绝不让它冒充"已校验的标定"。
    if d.get("player_track"):
        meta["player_track"] = d["player_track"]
    if d.get("player_track_source"):
        meta["player_track_source"] = d["player_track_source"]
    if d.get("sliding_calibration"):
        meta["sliding_calibration"] = d["sliding_calibration"]
        meta["sliding_anchors"] = d.get("sliding_anchors")
    if d.get("calibration_degeneracy"):
        meta["calibration_degeneracy"] = d["calibration_degeneracy"]
    if d.get("calibration_hoop_check"):
        meta["calibration_hoop_check"] = d["calibration_hoop_check"]
    if d.get("calibration_for_value"):
        meta["calibration_for_value"] = d["calibration_for_value"]
    if d.get("calibration_rejected"):
        meta["calibration_rejected"] = d["calibration_rejected"]
    # 自动逐帧标定的独立校验结论（拿人工标的篮筐当共时的真值点）
    if d.get("sliding_validation"):
        meta["sliding_validation"] = d["sliding_validation"]
    return meta


# 自动逐帧标定要能拿来做战术图，至少要达到这个质量（判据全部来自滑动标定自己的读数）
SLIDING_OK_RATIO_MIN = 0.5      # 锚点达标率（ratio >= FIT_RATIO_OK 的锚点占比）
SLIDING_MEDIAN_RATIO_MIN = 1.25  # 中位 ratio（= calibcheck.FIT_RATIO_BAD）


def _sliding_position_ok(meta: dict) -> tuple[bool, str]:
    """没有可用静态标定时，**自动逐帧标定**的球员坐标能不能用来出战术图？

    为什么需要这条兜底：真视频里静态标定经常不可用（镜头在动、或用户那份标定
    本身有问题），而逐帧滑动标定是每帧自动拟合球场线解出来的 H，实测在真实素材上
    中位 ratio 3.0 上下、达标率 0.95+。球员坐标其实是有的 —— 老版本只看静态标定的
    结论，于是"球员明明检测到了、战术页却一直空着"，用户完全不知道为什么。

    代价必须说清楚：滑动标定**没有经过独立校验**（没有画面里的真值点去核对），
    所以这里的结论只用于**展示性**产物（俯视战术图 / 阵型 / 间距），
    并且一律标注 `position_unverified=True`；计分/2-3 分判定仍然只走严格路径。
    """
    pt = meta.get("player_track") or {}
    n = int(pt.get("samples") or 0)
    # 走自动逐帧标定时，**必须**先过"手标篮筐"那道独立校验：
    # 实测这段素材的逐帧标定把篮筐投到 9.2m 外，却照样给出 481 帧战术图。
    sv = meta.get("sliding_validation") or {}
    if sv.get("checked") and not sv.get("ok"):
        return False, ("自动逐帧标定没通过独立校验：" + str(sv.get("reason") or ""))
    if n <= 0:
        return False, "这场没有球员轨迹（球员检测被关闭，或一个人都没检出）"
    if str(meta.get("player_track_source") or "") != "sliding_calibration":
        return False, ""
    sl = meta.get("sliding_calibration") or {}
    n_a = int(meta.get("sliding_anchors") or 0)
    ok_ratio = float(sl.get("ok_ratio") or 0.0)
    med = float(sl.get("median_ratio") or 0.0)
    if n_a < 2:
        return False, (f"自动逐帧标定只锁住 {n_a} 个锚点，不够")
    if ok_ratio < SLIDING_OK_RATIO_MIN or med < SLIDING_MEDIAN_RATIO_MIN:
        return False, ("自动逐帧标定质量不足（锚点达标率 %.2f、中位 ratio %.2f）—— "
                       "镜头运动/画面太糊时会出现这种读数" % (ok_ratio, med))
    # 成功的这条返回值会被拼进"位置未校验"的说明里，所以只给读数、不带前缀，
    # 免得出现"自动逐帧标定（自动逐帧标定（…））"这种套娃（实测拼出来过）。
    return True, ("%d 个锚点，达标率 %.2f，中位 ratio %.2f" % (n_a, ok_ratio, med))


def _tactics_coords_sane(player_track) -> tuple[bool, str]:
    """球员球场坐标在**物理上**说得通吗？（出战术图前的最后一道闸）

    为什么必须有这一道（用户实测）：自动逐帧标定自称 `median_ratio=14.5、
    达标率 0.999`，静态标定却是 `valid=False / rmse=23.5m`。战术层于是走了
    "自动标定"那条兜底路，**照样出了一张战术图** —— 而那张图上：

        x 分布: 90% 的点挤在 x=7.47~7.60（正好是边线 7.5）
        y 范围: -8.31 ~ 0.12（只有半个半场）

    90% 的球员"贴着同一条边线站"，物理上不可能 —— 那是 H 把画面挤成了一根线。
    分数（ratio/达标率）看不出来这种错，但**坐标自己的分布**能看出来。

    判据用两条与几何无关的硬约束：
      ① 贴边堆叠：某个轴的坐标里超过 70% 落在"边界 0.4m 以内"；
      ② 展布过窄：某个轴的展布 < 1.5m（球员不可能挤成一条线）。
    只在样本足够（≥120 个点）时才判定，免得短片段误杀。
    """
    xs = [float(p.x) for p in (player_track or [])]
    ys = [float(p.y) for p in (player_track or [])]
    n = len(xs)
    if n < 120:
        return True, ""
    W, L = 15.0, 28.0
    problems = []
    for name, vals, half in (("横向 x", xs, W / 2.0), ("纵向 y", ys, L / 2.0)):
        near_edge = sum(1 for v in vals if abs(abs(v) - half) <= 0.4)
        frac = near_edge / float(n)
        spread = max(vals) - min(vals)
        if frac > 0.70:
            problems.append("%s 有 %.0f%% 的点贴在边界 %.1fm 以内" % (name, frac * 100, half))
        elif spread < 1.5:
            problems.append("%s 只铺开 %.2fm" % (name, spread))
    if not problems:
        return True, ""
    return False, (
        "球员坐标在物理上说不通（%s）—— 这几乎一定是**标定不可用**造成的，"
        "不是球员真的挤在边线上。为避免给你一张错的俯视图，这次不出战术图。"
        "修法：在「上传与分析」页重新标一次球场，标完看「吻合度读数」是否 ≥1.25。"
        % "；".join(problems))


def _sliding_homography_sane(meta: dict) -> tuple[bool, str]:
    """自动逐帧标定给出的 H，投出来的坐标**落在合理球场范围内**吗？

    为什么必须有这一道（实测）：sliding 自称 median_ratio=10.96、达标率 1.000，
    但有 1080 个锚点的那次，把**画面四角**投进去得到：

        (0,0)→(7.2,-17.5)  (854,0)→(7.5,-76.9)
        (854,480)→(7.5,-13.5)  (0,480)→(6.3,-5.5)

    x 全挤在 6.3~7.5、y 跨度 71m（球场才 28m）—— 标定的目标点明明是
    折半坐标（x∈±7.5、y∈0~14），却投出这种结果，说明它锁定的四角是错的
    （实测那一帧四角只占画面 20.7%，是条细长对角带，根本不是半场）。

    判据（**故意定得很宽**）：只拦"离谱到不可能是相机视野"的情况。
    实测三种标定的画面四角落点范围：

        良好(auto t=60s)  |x|max  6.8   |y|max 12.7
        良好(t=5s 那帧)   |x|max 21.0   |y|max 55.9
        坏(事故那次 t=0)  |x|max  7.5   |y|max 76.8

    "良好"的那份也能到 55.9 —— 说明**这个判据区分能力有限**，定紧了会误杀。
    所以这里只拦 |x|>40 或 |y|>60 这种极端值，作为"早发现"的辅助；
    **真正的兜底是下游 _tactics_coords_sane**（直接看战术图要用的那批坐标，
    判据是"某轴 >70% 的点贴在边界上"，实测能准确拦住这份坏标定）。
    """
    sl = meta.get("sliding_calibration") or {}
    n_anchors = int(meta.get("sliding_anchors") or 0)
    if n_anchors <= 0:
        return True, ""                      # 没跑这条路径，不判
    anchors = sl.get("anchors") or []
    w = int(meta.get("width") or 0)
    h = int(meta.get("height") or 0)
    if not anchors or w <= 0 or h <= 0:
        # 没有锚点样本就没证据（老产物就是这样，只有统计量）——
        # **没有证据就不拦**，真正兜底的是下游 _tactics_coords_sane
        # （它只看坐标分布，不依赖锚点）。
        return True, ""
    try:
        from .court import (HALF_COURT_CORNERS, FULL_COURT_CORNERS,
                            apply_homography, find_homography)
        dst_c = (HALF_COURT_CORNERS if sl.get("half_court", True)
                 else FULL_COURT_CORNERS)
        checks = []
        for a in anchors:
            corners = a.get("corners")
            if not corners or len(corners) != 4:
                continue
            try:
                H = find_homography([list(p) for p in corners],
                                    [list(p) for p in dst_c])
            except Exception:                # noqa: BLE001
                continue
            for (px, py) in ((0, 0), (w, 0), (w, h), (0, h)):
                x, y = apply_homography(H, px, py)
                checks.append((float(x), float(y)))
        if not checks:
            return True, ""                  # 算不出 -> 无证据，不拦
        bad = [(x, y) for (x, y) in checks if abs(x) > 40 or abs(y) > 60]
    except Exception:                        # noqa: BLE001
        return True, ""                      # 判不了就不拦，交给下游其他检查
    if not bad:
        return True, ""
    f = bad[0]
    return False, (
        "自动逐帧标定给出的坐标**离谱到不可能是球场**（把画面四角投进去得到 "
        "x=%.1f、y=%.1f；球场只有 15m×28m，相机视野再宽也到不了这个量级）。"
        "说明它锁定的球场四角是错的。球员位置会被压到边线上，"
        "这份自动标定不能用来出战术图。" % (f[0], f[1]))


def _hoop_next_step(meta: dict) -> str:
    """没检测到篮筐时，给出**可执行的下一步**（而不是只说"没有可用篮筐"）。

    为什么需要（用户实测 2026-10-06）：那次报告只写了一句
    「视觉路径：没有检测到可用篮筐，无法确认投篮结果」，然后就是一片 0 ——
    用户看到的是"战术图、投篮热区啥都没检测出来"，完全不知道该怎么办。

    完整因果链（查过真实产物）：
        篮筐没标定/没识别出 → 判不出出手（0 次）
            ├─→ 投篮热区：空（没有出手可画）
            └─→ 战术图：另受标定吻合度卡住
    所以这里把"标一次篮筐"这一步明确写出来 —— 它是这条链上**最便宜的一环**。
    """
    reasons = " ".join(str(x) for x in
                       ((meta.get("visual") or {}).get("reasons") or []))
    no_hoop = ("篮筐" in reasons) or (not reasons and
                                      not (meta.get("hoop") or
                                           meta.get("hoop_px")))
    if not no_hoop:
        return ""
    return ("**下一步（最省事的一环）**：在「上传与分析」页点**「标篮筐」**，"
            "在画面上点一下篮筐中心即可 —— 它只需要**一个点**。"
            "标完之后重新分析，出手、命中、投篮热区才会出来；"
            "不标的话，这一整条链（出手判定 → 命中 → 热区）都无从计算。")


def _judgement(meta: dict, shots: list) -> dict:
    """这次到底「判出来了」还是「判不了」，以及为什么。

    存在的意义：以前判不出来的时候，产物里就是一个 0 : 0，界面上写着「平局」。
    那是在误导人 —— **0:0 表示"没检测到得分"，不等于"双方都没得分"**。
    这里把状态和原因显式记下来，战报和界面就能说人话。
    """
    sb = meta.get("scoreboard") or {}
    vis = meta.get("visual") or {}
    if meta.get("source") == "synthetic":
        return {"state": "synthetic", "note": "合成数据（demo），比分是构造出来的"}
    policy = str(meta.get("score_policy", "scoreboard") or "scoreboard").lower()
    if policy in ("court", "visual", "auto"):
        made = [s for s in shots if s.made]
        if made:
            note = (f"按场上检测到的进球计分：本片段 {len(made)} 次命中，"
                    f"合计 {sum(s.points for s in made)} 分。")
            fin = meta.get("scoreboard_final") or {}
            if fin:
                note += (f" 比分牌读数 {fin.get('home', 0)} : {fin.get('away', 0)}"
                         " 仅作参考，不计入本片段得分。")
            return {"state": "court", "note": note}
        # 一次命中都没有：必须分清「判了，这段确实没进球」和「根本没法判」。
        # 否则界面上就只剩一个 0 分，用户分不出"没得分"和"识别没跑起来"
        # （实测踩到：夜间手持素材的篮筐检测漂移 781px、视觉判定被主动放弃，
        #  产物里却只有一句"本片段未检测到进球"，等于把失败藏进了 0 分）。
        reasons = _no_score_reasons(meta, sb, include_scoreboard=False)
        _nxt = _hoop_next_step(meta)
        if meta.get("visual_error"):
            return {"state": "cannot_judge",
                    "note": "视觉路径没能跑起来，本片段无法判定有没有进球。",
                    "reasons": reasons,
                    "next_step": _nxt}
        return {"state": "court", "note": "本片段未检测到进球。",
                "reasons": reasons, "next_step": _nxt}
    if sb.get("frames_hit"):
        unmatched = [s for s in shots if s.made and not s.counts_for_score]
        counted = [s for s in shots if s.made and s.counts_for_score]
        note = f"比分由广播比分牌给出（读到 {sb.get('frames_hit')} 帧）"
        if unmatched:
            note += (f"；视觉路径另有 {len(unmatched)} 次命中未被比分牌确认，"
                     "未计入总分")
        elif counted:
            note += f"；另有 {len(counted)} 次视觉命中已计入总分"
        return {"state": "scoreboard", "note": note}
    if shots:
        return {"state": "visual",
                "note": f"由「球 + 篮筐」路径判出 {len(shots)} 次出手"}
    reasons = _no_score_reasons(meta, sb)
    unmatched = [s for s in shots if s.made and not s.counts_for_score]
    if unmatched:
        return {"state": "visual_unconfirmed",
                "note": (f"视觉路径检测到 {len(unmatched)} 次命中，但比分牌在本片段"
                         "没有对应得分事件（可能是回放/误检），因此没有计入总分；"
                         "命中已保留在高光和报告里，可在复核页确认。"),
                "reasons": reasons}
    return {"state": "cannot_judge",
            "note": "本片段没有识别到任何得分事件",
            "reasons": reasons}


def _no_score_reasons(meta: dict, sb: dict, include_scoreboard: bool = True) -> list:
    """一次得分都没判出来时，把「为什么」一条条列清楚。

    存在的意义：只给一个 0 分，用户分不出「这段真没进球」和「识别根本没跑
    起来」。实测踩到：夜间手持素材里篮筐检测漂移了 781 像素、视觉判定被主动
    放弃，产物里却只有一句"未检测到进球"。

    include_scoreboard=False 用在 court/visual 口径上 —— 那个口径下比分牌
    本来就不参与计分，把"比分牌未启用"列成失败原因是噪音。
    """
    reasons = []
    if include_scoreboard:
        if meta.get("scoreboard_error"):
            reasons.append("比分牌：" + str(meta["scoreboard_error"]).split("\n")[0][:90])
        elif sb.get("frames_read"):
            # 读到了帧但一帧都没命中：说清"是台标不匹配"，并给出**最省事的做法**。
            # 以前只说"和已标定的模板不匹配"，用户不知道下一步该干什么
            # （实测："按计分板判断进球的功能没了"，其实是要框选一下）。
            _act = str(meta.get("scoreboard_action") or "").strip()
            reasons.append(
                f"比分牌：一帧都没读出来（0/{sb['frames_read']}）——"
                "这段视频的台标和已标定的模板不匹配。"
                + ("最省事的做法：" + _act if _act else
                   "最省事的做法：在「上传与分析」页点**「框选比分牌」**，"
                   "在画面上把比分牌拖一个框圈起来，再读一次。"))
        else:
            reasons.append("比分牌：未启用")
    if meta.get("visual_error"):
        reasons.append("视觉路径：" + str(meta["visual_error"]).split("。")[0][:90])
    return reasons


def _team_name(rt: RawTrack, team: str) -> str:
    """仅使用队名元数据；缺失时使用主队/客队，不从球员名字推测。"""
    named = (rt.detections_meta.get("team_names") or {}).get(team)
    if named and not re.fullmatch(r"(?:球员|[Tt])\d+|其他球员", named):
        return named
    return "主队" if team == "home" else "客队"


def _shot_event(s: Shot, rt: RawTrack) -> dict:
    name = rt.players[s.player_id].name if s.player_id in rt.players else s.player_id
    return {"t": round(s.t, 2), "team": s.team, "player_id": s.player_id,
            "player": name, "value": s.value,
            "made": None if s.result == "unknown" else s.made, "result": s.result,
            "points": s.score_points, "counts_for_score": s.counts_for_score,
            "value_estimated": "value_estimated" in s.tags,
            "value_assumed": "value_assumed" in s.tags,
            "location_unknown": "location_unknown" in s.tags,
            "clip_start": s.clip_start, "clip_end": s.clip_end,
            "decision_t": s.decision_t, "release_source": s.release_source,
            "x": round(s.x, 2), "y": round(s.y, 2),
            "zone": "位置未知" if "location_unknown" in s.tags else zone_of(s.x, s.y), "confidence": s.confidence,
            "source": s.outcome_source, "period": s.period,
            "tags": s.tags, "evidence": s.evidence,
            "crossing_t": s.crossing_t, "review_t": s.review_t, "suggested_made": s.suggested_made}
