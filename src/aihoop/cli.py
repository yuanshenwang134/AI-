"""命令行入口 —— 不启服务也能一键跑完整条管线。

用法示例（Windows PowerShell，注意在仓库根目录执行）：

  # 1) 合成一场比赛并跑完整管线（零依赖，用来验证链路与前端）
  python -m aihoop.cli demo --seed 7 --out out/demo

  # 2) 从已有的 raw_track.json 重跑（调参不用重新推理）
  python -m aihoop.cli run --raw out/demo/raw_track.json --out out/run1

  # 3) 真视频（需要 ultralytics + opencv；先用 4 个角点做标定）
  python -m aihoop.cli calibrate --video data/mygame.mp4 --corners 320,880 1600,880 1600,300 320,300 --half-court --save out/cal.json
  python -m aihoop.cli video --video data/mygame.mp4 --cal out/cal.json --out out/mygame

  # 4) 只重算统计（改了规则阈值后）
  python -m aihoop.cli rescore --raw out/demo/raw_track.json --out out/rescore
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


def _add_common(p: argparse.ArgumentParser) -> None:
    p.add_argument("--out", default="out/run", help="输出目录")
    p.add_argument("--no-highlights", action="store_true", help="不生成高光片段")
    p.add_argument("--highlight-limit", type=int, default=15)
    p.add_argument("--review-threshold", type=float, default=0.6,
                   help="低于该置信度进入人工复核")


def _cfg(args, video_path=None):
    from .pipeline import PipelineConfig
    from .rules import RulesConfig
    return PipelineConfig(
        out_dir=args.out,
        rules=RulesConfig(review_threshold=args.review_threshold),
        make_highlights=not args.no_highlights,
        highlight_limit=args.highlight_limit,
        video_path=video_path,
    )


def _progress(p: float, msg: str) -> None:
    bar = int(p * 30)
    sys.stdout.write(f"\r[{'#' * bar}{'.' * (30 - bar)}] {p*100:5.1f}% {msg:<40}")
    sys.stdout.flush()
    if p >= 1.0:
        sys.stdout.write("\n")


def cmd_demo(args) -> int:
    from .sources import synthetic_game
    from .pipeline import run_pipeline
    rt = synthetic_game(seed=args.seed, duration=args.duration)
    raw_path = Path(args.out) / "raw_track.json"
    raw_path.parent.mkdir(parents=True, exist_ok=True)
    rt.save(str(raw_path))
    print(f"[ok] 合成检测结果已保存：{raw_path}")
    res = run_pipeline(rt, _cfg(args), progress=_progress)
    print("[ok] " + res.summary())
    print(f"[ok] 战报：{Path(args.out) / 'report.md'}")
    print(f"[ok] 打开前端：web/index.html（演示数据模式下可直接看）")
    return 0


def cmd_run(args) -> int:
    from .sources import jsonl_source
    from .pipeline import run_pipeline
    rt = jsonl_source(args.raw)
    res = run_pipeline(rt, _cfg(args), progress=_progress)
    print("[ok] " + res.summary())
    return 0


def cmd_rescore(args) -> int:
    return cmd_run(args)


def cmd_calcheck(args) -> int:
    """客观校验一份标定能不能用在某段视频上。

    这是真视频链路的**前置关卡**：单应标定只对固定机位成立，标错了不会报错、
    只会把球员投到错的位置。所以先量两个指标：
      1. 机位稳不稳（相位相关测帧间位移）
      2. 标定准不准（把球场线投回画面，量"这儿是不是真有一条白线"）
    并输出叠加图给人复核。
    """
    import json
    from .court import Calibration
    from .calibcheck import camera_motion, court_fit_score, render_overlay

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    motion = camera_motion(args.video)
    print(f"[机位] 帧间位移中位数 {motion.get('median_px')} px/s"
          f"（最大 {motion.get('max_px')}）-> {motion.get('verdict')}")
    print(f"       {motion.get('note')}")
    if motion.get("verdict") == "moving":
        print("[警告] 镜头在明显移动：**一次标定的单应矩阵不可用**，"
              "球员位置会整体错位。请用固定机位素材，或改用逐帧球场关键点方案。")

    report = {"video": args.video, "motion": motion}
    if args.cal and Path(args.cal).exists():
        cal = Calibration.load(args.cal)
        print(f"[标定] {args.cal}  适用本视频(文件名绑定)？"
              f"{cal.matches_video(args.video, *(_vid_size(args.video)) )}")
        fit = court_fit_score(args.video, cal)
        report["calibration"] = fit
        print(f"[吻合] ratio = {fit.get('ratio')}  ok = {fit.get('ok')}")
        print(f"       {fit.get('note')}")
        for fr in fit.get("frames", []):
            print(f"       t={fr['t']:>6}s  线亮脊 {fr['on_line']:>5}  "
                  f"地面 {fr['floor']:>5}  比值 {fr['ratio']:>5}  {fr.get('lines')}")
        for k, t in enumerate([0.05, 0.5, 0.9]):
            img = render_overlay(args.video, cal, t * motion.get("duration", 30.0),
                                 str(out / f"overlay_{k}.jpg"))
            if img:
                print(f"[图]   {img}")
    else:
        print(f"[标定] 没找到 {args.cal}，跳过吻合度检查")
    (out / "calcheck.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[ok] 报告：{out / 'calcheck.json'}")
    return 0


def _vid_size(video_path: str) -> tuple:
    """读视频宽高（只用 cv2；读不到就返回 (0,0)）。"""
    try:
        import cv2
        c = cv2.VideoCapture(video_path)
        w, h = int(c.get(3)), int(c.get(4))
        c.release()
        return w, h
    except Exception:  # noqa: BLE001
        return 0, 0


def cmd_tactics(args) -> int:
    """只重算战术层 —— 调阈值时不用重跑检测。

    和 `rescore`（只重算统计）是一个思路：把"检测"和"判读"解耦。
    改 PossessRadius / ManDist 这类经验阈值时，检测结果没变，
    重跑一遍 YOLO 是纯浪费；直接从 raw_track.json 重算战术层即可。
    """
    from .sources import jsonl_source
    from .tactics import (TacticsConfig, build_tactics, build_tactics_frames,
                          write_tactics_frames)
    from .export import write_passes_csv, write_spacing_csv
    from .rules import build_shots

    rt = jsonl_source(args.raw)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    cfg = TacticsConfig(frame_step=args.frame_step)
    shots = build_shots([a.to_dict() for a in rt.attempts],
                        ball_track=rt.ball_track,
                        scoreboard_events=rt.scoreboard_events)
    t = build_tactics(rt.player_track, rt.ball_track, rt.players, shots,
                      duration=rt.duration, cfg=cfg, progress=_progress)
    (out / "tactics.json").write_text(
        json.dumps(t, ensure_ascii=False, indent=2), encoding="utf-8")
    if t.get("available"):
        frames = build_tactics_frames(rt.player_track, rt.ball_track,
                                      duration=rt.duration, cfg=cfg)
        write_tactics_frames(str(out / "tactics_frames.jsonl"), frames)
        write_passes_csv(str(out / "passes.csv"), t)
        write_spacing_csv(str(out / "spacing.csv"), t)
    print(f"[ok] 战术层：{'可用' if t.get('available') else '不可用'}"
          f"（{t.get('reason') or '正常'}）")
    print(f"[ok] 产物：{out / 'tactics.json'}")
    return 0


def cmd_calibrate(args) -> int:
    if getattr(args, "interactive", False):
        if not args.video:
            print("[err] --interactive 需要同时给 --video")
            return 2
        from .calibcheck import interactive_calibrate
        cal = interactive_calibrate(args.video, args.save,
                                    half_court=args.half_court,
                                    at_second=getattr(args, "at", 0.0))
        if cal is None:
            print("[cancel] 已取消，未保存")
            return 1
        print(f"[ok] 标定已保存：{args.save}（重投影误差 {cal.reproj_error_m} m）")
        print("     建议接着跑：python -m aihoop.cli calcheck --video "
              f"{args.video} --cal {args.save}")
        return 0
    from .court import calibrate_from_corners
    pts = []
    for tok in args.corners:
        x, _, y = tok.partition(",")
        pts.append((float(x), float(y)))
    if len(pts) != 4:
        print("[error] 需要正好 4 个角点：x,y x,y x,y x,y", file=sys.stderr)
        return 2
    cal = calibrate_from_corners(pts, half_court=args.half_court, name=args.name,
                                 video_path=args.video or "")
    Path(args.save).parent.mkdir(parents=True, exist_ok=True)
    cal.save(args.save)
    print(f"[ok] 标定已保存：{args.save}")
    if cal.for_video:
        print(f"     这份标定绑定到视频：{cal.for_video}"
              "（只对这个机位/文件名生效，换视频会自动拒绝使用）")
    print(f"     重投影误差 {cal.reproj_error_m} m "
          f"({'良好' if cal.reproj_error_m < 0.2 else '可用' if cal.reproj_error_m < 0.5 else '偏大，建议重点'})")
    if args.video:
        from .highlight import probe_info
        print(f"     视频信息：{probe_info(args.video)}")
    return 0


def _parse_basket_teams(s):
    """解析 `--basket-teams "1=home,2=away,3=home"` → {进球序号: 队别}。

    序号从 1 开始，按时间排序。给不出/写错就抛 ValueError，让人一眼看出问题，
    而不是悄悄忽略、让用户以为改判生效了。
    """
    if not s:
        return None
    out = {}
    for tok in str(s).replace("，", ",").split(","):
        tok = tok.strip()
        if not tok:
            continue
        if "=" not in tok:
            raise ValueError(f"--basket-teams 片段没有 '='：{tok!r}")
        k, _eq, val = tok.partition("=")
        try:
            idx = int(k.strip())
        except Exception as e:
            raise ValueError(f"--basket-teams 序号不是整数：{k!r}") from e
        val = val.strip().lower()
        if val not in ("home", "away"):
            raise ValueError(f"--basket-teams 队别只能是 home/away：{val!r}")
        if idx < 1:
            raise ValueError("--basket-teams 序号从 1 开始")
        out[idx] = val
    return out or None


def _parse_hoop_hint(s):
    if not s:
        return None
    try:
        parts = [float(v) for v in str(s).replace("，", ",").split(",")]
        if len(parts) == 2:
            return (parts[0], parts[1], 25.0)
        if len(parts) >= 3:
            return (parts[0], parts[1], parts[2])
    except Exception:
        pass
    raise ValueError("--hoop 格式应为 x,y 或 x,y,r，例如 --hoop 560,332,40")


def cmd_video(args) -> int:
    from .court import Calibration
    from .sources import VideoSource
    from .pipeline import run_pipeline
    if args.cal:
        cal = Calibration.load(args.cal)
    elif args.fast and args.score_policy == "scoreboard":
        cal = Calibration(name="unavailable", method="unavailable")
    else:
        raise ValueError("此分析需要 --cal；仅比分模式可使用 --fast --score-policy scoreboard")
    print(f"[info] 标定 {cal.name}，重投影误差 {cal.reproj_error_m} m")
    # 「篮下判进球」的阈值：默认配置 + 命令行覆盖
    sight_cfg = None
    if not getattr(args, "no_hoop_sight", False):
        from .hoopsight import SightConfig
        sight_cfg = SightConfig()
        md = getattr(args, "hoop_sight_min_drop", None)
        if md is not None:
            sight_cfg.min_drop_ry = float(md)
        mc = getattr(args, "hoop_sight_min_conf", None)
        if mc is not None:
            sight_cfg.min_confidence = float(mc)

    # 人工标注白名单（scripts/label_baskets.py 的产出）
    basket_labels = None
    labels_path = getattr(args, "basket_labels", None)
    if labels_path:
        lp = Path(labels_path)
        if not lp.exists():
            print(f"[err] 找不到标注文件：{lp}")
            return 2
        rows = []
        for line in lp.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line:
                rows.append(json.loads(line))
        n_made = sum(1 for r in rows if r.get("label") == "made")
        basket_labels = {"path": str(lp), "rows": rows}
        print(f"[info] 人工标注白名单：{lp.name} —— {len(rows)} 条候选，"
              f"其中 {n_made} 条标为进球（自动判定中不在此列的将被视为误报）")
    # 人工指定「第几个进球是哪一队」：自动分队判不出时的兜底
    teams_override = _parse_basket_teams(getattr(args, "basket_teams", None))
    # 「向前找」秒数：判某球是哪一队时看进球前这段时间谁离筐最近
    lookback_s = float(getattr(args, "basket_lookback", 1.5) or 0.0)
    # 人工标点（scripts/mark_landmarks.py）：直接给出篮筐，跳过检测器
    manual_hoop = None
    marks_path = getattr(args, "marks", None)
    if marks_path:
        from .hoop import Hoop
        mp = Path(marks_path)
        if not mp.exists():
            print(f"[err] 找不到标点文件：{mp}")
            return 2
        d = json.loads(mp.read_text(encoding="utf-8"))
        hp = d.get("hoop")
        if not hp:
            print(f"[err] {mp} 里没有 hoop 字段"
                  "（用 scripts\\mark_landmarks.py 生成）")
            return 2
        manual_hoop = Hoop(cx=float(hp[0]), cy=float(hp[1]),
                           rx=float(hp[2]), ry=float(hp[3]),
                           votes=1, confidence=1.0, method="manual",
                           t=float(d.get("at") or 0.0))
        print(f"[info] 用人工标定的篮筐：cx={manual_hoop.cx:.0f} "
              f"cy={manual_hoop.cy:.0f} rx={manual_hoop.rx:.0f} "
              f"ry={manual_hoop.ry:.0f}（来自 {mp.name}）")
    # 进球判定模型（学到的小 CNN）：有就用，没有就退回手写判据
    basket_model = None
    bm_path = getattr(args, "basket_model", None)
    if bm_path:
        try:
            from .basket_model import BasketScorer
            basket_model = BasketScorer.load(bm_path)
            if basket_model is not None:
                info = basket_model.summary()
                print(f"[info] 进球判定模型：{bm_path}"
                      f"（留一法 AUC {info.get('loo_auc_cnn')}，"
                      f"阈值 {info.get('threshold'):.3f}）")
        except Exception as e:  # noqa: BLE001
            print(f"[warn] 进球判定模型加载失败，退回手写判据：{e}")

    # ---- 界面/命令行的「逐球确认」反馈：自动生效 ----
    # 用户在高光页把某球判成"没进"，就写进 data/basket_feedback.jsonl。
    # 这里默认读它（不需要任何参数）—— 否则"我明明判过了，怎么还报错进球"
    # 会反复发生（实测踩过：CLI 这条路径之前根本不读反馈）。
    if basket_labels is None:
        fb_file = Path("data/basket_feedback.jsonl")
        if fb_file.exists():
            try:
                rows = []
                for line in fb_file.read_text(encoding="utf-8").splitlines():
                    line = line.strip()
                    if not line:
                        continue
                    d = json.loads(line)
                    if Path(str(d.get("video", ""))).name != Path(args.video).name:
                        continue
                    t = float(d.get("t", 0.0))
                    rows.append({"t0": t - 1.0, "t1": t + 1.0,
                                 "label": str(d.get("label", ""))})
                if rows:
                    basket_labels = {"path": str(fb_file), "rows": rows}
                    n_made = sum(1 for r in rows if r["label"] == "made")
                    print(f"[info] 你的逐球确认（{fb_file.name}）：{len(rows)} 条，"
                          f"其中 {n_made} 个判为进球；其余自动判定视为误报")
            except Exception as e:  # noqa: BLE001
                print(f"[warn] 读反馈文件失败，忽略：{e}")

    src = VideoSource(args.video, cal, weights=args.weights, device=args.device,
                      stride=args.stride, player_stride=args.player_stride,
                      scoreboard=not args.no_scoreboard,
                      detect_players=not args.fast,
                      home_hoop=args.home_hoop,
                      score_policy=getattr(args, "score_policy", "court"),
                      visual_shot_value=getattr(args, "visual_shot_value", 2),
                      hoop_hint=_parse_hoop_hint(getattr(args, "hoop", None)),
                      hoop_weights=getattr(args, "hoop_weights", "") or "",
                      ball_weights=getattr(args, "ball_weights", "") or "",
                      hoop_sight_cfg=sight_cfg,
                      basket_teams=teams_override,
                      basket_lookback_s=lookback_s,
                      scoreboard_events=getattr(args, "scoreboard_events", None),
                      manual_hoop=manual_hoop,
                      basket_labels=basket_labels,
                      basket_model=basket_model,
                      auto_sliding=not getattr(args, "no_auto_sliding", False),
                      sliding_stride=getattr(args, "sliding_stride", 5),
                      sliding_max_seconds=getattr(args, "sliding_max_seconds", 0.0))
    if args.fast:
        print("[info] --fast：跳过逐帧 YOLO，只读比分牌 + 颜色线索追球"
              "（CPU 上快很多；球员个体统计会缺失）")
    print("[info] 开始推理（CPU 上会比较慢，建议先用 demo 跑通链路）…")
    rt = src.run(progress=lambda p, m="推理中": _progress(p, m))
    raw = Path(args.out) / "raw_track.json"
    raw.parent.mkdir(parents=True, exist_ok=True)
    rt.save(str(raw))
    print(f"[ok] 检测结果：{raw}")

    sb = rt.detections_meta.get("scoreboard")
    if sb and sb.get("frames_hit"):
        print(f"[ok] 比分牌：读到 {sb['frames_hit']}/{sb['frames_read']} 帧，"
              f"最终 {rt.detections_meta.get('final_score')}")
    elif sb:
        # 几何有了、模板也有，但一帧都没匹配上 —— 这是「读不出来」，不是「没有牌」
        print(f"[warn] 比分牌读不出来：扫了 {sb.get('frames_read')} 帧，"
              "命中 0 帧（多半是模板没标定，或台标样式和模板不符）")
        print("       先标定模板：python scripts\\build_scoreboard_templates.py "
              "--video <视频>")
    elif rt.detections_meta.get("scoreboard_error"):
        print(f"[warn] 比分牌没读出来：{rt.detections_meta['scoreboard_error']}")
    elif rt.detections_meta.get("scoreboard_absent"):
        print("[info] 画面里没有广播比分牌覆盖层 —— 自动计分改走"
              "「球 + 篮筐」视觉路径")
        print("       这段视频里只有比赛计时（LED 上只有时间和 24 秒），"
              "没有比分数字可读")

    hs = rt.detections_meta.get("hoopsight")
    if hs:
        shots = hs.get("shots") or []
        jc = rt.detections_meta.get("jersey_colors") or {}
        legend = jc.get("legend") or {}
        lconf = jc.get("legend_confidence") or {}
        if legend:
            parts = []
            for k, v in legend.items():
                c = lconf.get(k, "")
                parts.append(f"{k}={v}" + ("（颜色不确定）" if c == "uncertain"
                                           else ""))
            print("[info] 球衣颜色对照（分队依据）：" + "，".join(parts))
        if shots:
            diag = hs.get("diagnosis") or {}
            if diag and not diag.get("judge_reliable", True):
                # 判据在这段素材上没有判别力 —— 先警告，再报数字
                print(f"[warn] ⚠ 自动判定在这段素材上**不可信**：{diag.get('reason')}")
            when = "、".join(f"{s['t']:.1f}s" for s in shots[:8])
            print(f"[ok] 篮下判进球：判出 {len(shots)} 次进球（{when}）"
                  " —— 不依赖比分牌，也不依赖球检测器")
            ta = (hs.get("team_attribution") or {}).get("by_shot") or {}
            n_unknown = 0
            for i, s in enumerate(shots):
                info = ta.get(str(i)) or ta.get(i) or {}
                if not info:
                    continue
                team = info.get("team", "")
                tag = {"home": "主队", "away": "客队"}.get(team, "未定")
                if not team:
                    n_unknown += 1
                print(f"       第 {i + 1} 球 t={s['t']:.2f}s → {tag}"
                      f"（{info.get('confidence', '?')}）：{info.get('reason', '')}")
            if n_unknown:
                n_people = len(hs.get("people") or [])
                print(f"[warn] {n_unknown} 次进球判不出是哪一队进的 —— "
                      f"代码在篮筐附近专门找过「人形运动块」，全片检出 "
                      f"{n_people} 个"
                      + ("（这段素材筐下确实没人，不是没检）"
                         if n_people == 0 else ""))
                print("       想人工定队：--basket-teams \"1=home,2=away,3=home\"")
            if hs.get("people_sheet"):
                print(f"       筐下找人核对图：{hs['people_sheet']}")
            if hs.get("verify_sheet"):
                print(f"       逐帧验证图：{hs['verify_sheet']}")
            if hs.get("team_sheet"):
                print(f"       分队核对图（筐下球员 + 队别/球衣色）："
                      f"{hs['team_sheet']}")
        elif not rt.detections_meta.get("scoreboard"):
            print(f"[info] 篮下判进球：没判到进球（{hs.get('note')}）")
        cv = hs.get("calibration_for_value") or {}
        if shots and not cv.get("ok"):
            print(f"[warn] 分值只能按经验假设（默认 "
                  f"{getattr(args, 'visual_shot_value', 2)} 分）：{cv.get('reason')}")
    elif rt.detections_meta.get("hoopsight_error"):
        print(f"[warn] 篮下判进球没跑起来："
              f"{rt.detections_meta['hoopsight_error']}")

    if not rt.attempts:
        print("[warn] 没有提取到任何出手。检查一下：")
        if rt.detections_meta.get("scoreboard_absent") or \
                rt.detections_meta.get("scoreboard_error"):
            print("       ① 这段视频没有比分牌 → 只能靠球轨迹：确认球检测权重"
                  "（--ball-weights）与篮筐权重（--hoop-weights）都给了")
            print("       ② 球轨迹没连成投篮弧线时，用 --attempts 提供人工标注"
                  "的出手时刻（半自动兜底）")
        else:
            print("       ① 这段视频有没有比分牌覆盖层（有的话先标定模板）")
            print("       ② 或者用 --attempts 提供人工标注的出手时刻（半自动兜底）")
        if args.attempts:
            _load_attempts(rt, args.attempts)
    res = run_pipeline(rt, _cfg(args, video_path=args.video), progress=_progress)
    print("[ok] " + res.summary())
    return 0


def _load_attempts(rt, path: str) -> None:
    """载入人工标注/补录的出手。

    JSON: 直接给 Attempt 字段，例如
      [{"t": 3.8, "team": "home", "value": 2, "made": true}]
    CSV 列：t,team,player_id,x,y,value,is_free_throw,period
      没有 value 时，is_free_throw=true -> 1 分，否则默认 2 分。
    人工补录默认 made=true、置信度 1.0、计入总分。
    """
    import csv
    from .sources import Attempt

    def apply_manual_defaults(d: dict) -> dict:
        d = dict(d)
        val = d.pop("value", None)
        if val is None:
            val = 1 if d.get("is_free_throw") else 2
        try:
            val = int(val)
        except Exception:
            val = 2
        d["forced_value"] = val
        d["is_free_throw"] = (val == 1)
        d.setdefault("made", True)
        d.setdefault("conf", 1.0)
        d["source"] = "manual"
        d["location_source"] = "manual"
        d["location_estimated"] = False
        d["counts_for_score"] = True
        d["value_source"] = "manual"
        d["manual"] = True
        return d

    if path.endswith(".json"):
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        for d in data:
            rt.attempts.append(Attempt(**apply_manual_defaults(d)))
    else:
        with open(path, encoding="utf-8-sig", newline="") as f:
            for r in csv.DictReader(f):
                d = {
                    "t": float(r["t"]),
                    "team": r.get("team", "home") or "home",
                    "player_id": r.get("player_id", "MANUAL") or "MANUAL",
                    "x": float(r.get("x", 0) or 0),
                    "y": float(r.get("y", 0) or 0),
                    "period": int(float(r.get("period", 1) or 1)),
                    "is_free_throw": str(r.get("is_free_throw", "")).lower()
                    in ("1", "true", "yes"),
                }
                if r.get("value") not in (None, ""):
                    d["value"] = int(float(r["value"]))
                rt.attempts.append(Attempt(**apply_manual_defaults(d)))
    print(f"[ok] 载入 {len(rt.attempts)} 条人工出手/进球标注")

def cmd_serve(args) -> int:
    import uvicorn
    uvicorn.run("aihoop.api:app", host=args.host, port=args.port,
                reload=args.reload)
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="aihoop", description="AI 篮球分析软件 · 命令行")
    sub = p.add_subparsers(dest="cmd", required=True)

    d = sub.add_parser("demo", help="合成一场比赛并跑完整管线（零依赖）")
    d.add_argument("--seed", type=int, default=7)
    d.add_argument("--duration", type=float, default=720.0, help="比赛时长（秒）")
    _add_common(d)
    d.set_defaults(func=cmd_demo)

    r = sub.add_parser("run", help="从 raw_track.json 重跑管线")
    r.add_argument("--raw", required=True)
    _add_common(r)
    r.set_defaults(func=cmd_run)

    s = sub.add_parser("rescore", help="只重算统计（调阈值用）")
    s.add_argument("--raw", required=True)
    _add_common(s)
    s.set_defaults(func=cmd_rescore)

    t = sub.add_parser("tactics", help="只重算战术层（调阈值用）")
    t.add_argument("--raw", required=True, help="raw_track.json")
    t.add_argument("--frame-step", type=float, default=1.0,
                   help="战术帧采样间隔（秒）")
    t.add_argument("--out", default="out/tactics")
    t.set_defaults(func=cmd_tactics)

    c = sub.add_parser("calibrate", help="用 4 个角点做球场标定")
    c.add_argument("--corners", nargs=4, required=True,
                   metavar="X,Y", help="4 个角点像素坐标，顺序：底线左 底线右 中线右 中线左")
    c.add_argument("--half-court", action="store_true")
    c.add_argument("--name", default="court")
    c.add_argument("--save", required=True)
    c.add_argument("--video", default=None)
    c.add_argument("--interactive", action="store_true",
                   help="在视频帧上点 4 个角点（推荐：手输像素坐标误差太大）")
    c.add_argument("--at", type=float, default=0.0,
                   help="用第几秒的帧来标定（--interactive）")
    c.set_defaults(func=cmd_calibrate)

    cc = sub.add_parser("calcheck", help="校验标定是否适用于某段视频（战术图前置关卡）")
    cc.add_argument("--video", required=True)
    cc.add_argument("--cal", default="data/calibration.json")
    cc.add_argument("--out", default="out/calcheck")
    cc.set_defaults(func=cmd_calcheck)

    v = sub.add_parser("video", help="真视频推理（需要 ultralytics/opencv）")
    v.add_argument("--video", required=True)
    v.add_argument("--cal", default=None, help="标定文件；仅比分模式 --fast --score-policy scoreboard 可省略")
    v.add_argument("--weights", default="yolov8n.pt")
    v.add_argument("--device", default=None, help="cpu / 0 / 0,1")
    v.add_argument("--stride", type=int, default=1,
                   help="已废弃：球追踪始终逐帧；请用 --player-stride 控制球员检测抽帧")
    v.add_argument("--player-stride", type=int, default=2,
                   help="球员检测的抽帧步长（球追踪始终逐帧，两者要求不同）")
    v.add_argument("--attempts", default=None, help="人工出手标注 CSV/JSON（半自动兜底）")
    v.add_argument("--no-scoreboard", action="store_true",
                   help="不读广播比分牌（默认会读，这是自动计分的主证据）")
    v.add_argument("--fast", action="store_true",
                   help="跳过逐帧 YOLO，只要比分牌 + 颜色追球（CPU 上快十几倍）")
    v.add_argument("--home-hoop", default="left", choices=["left", "right"],
                   help="主队进攻哪一侧篮筐（比分牌给不出方向，用它定出手位置）")
    v.add_argument("--score-policy", default="auto",
                   choices=["court", "visual", "scoreboard", "auto"],
                   help="计分口径：auto（默认）=比分牌真读出来了就用它（带入分+事件），"
                        "否则按场上进球；court/visual=一律按场上进球；"
                        "scoreboard=强制用比分牌带入+事件")
    v.add_argument("--visual-shot-value", type=int, default=2, choices=[2, 3],
                   help="无标定时 2/3 分：默认 2=自动估计并回退 2；3=强制按 3 分")
    v.add_argument("--hoop-weights", default="",
                   help="训练好的篮筐检测权重（scripts/label_rim.py 标注 + 训练）")
    v.add_argument("--no-hoop-sight", action="store_true",
                   help="关掉「篮下判进球」那条路（默认开启：没有比分牌时"
                        "它是不靠球检测器的自动计分主证据）")
    v.add_argument("--hoop-sight-min-drop", type=float, default=None,
                   help="篮下判进球：总下落量的下限（× 篮圈半高），默认 1.5")
    v.add_argument("--basket-teams", default=None,
                   help="人工指定第 N 个进球是哪一队：如 \"1=home,2=away,3=home\"。"
                        "用于自动分队判不出的素材（筐下检不到球员时）")
    v.add_argument("--basket-lookback", type=float, default=1.5,
                   help="「向前找」多少秒：判某球是哪一队时，看进球前这么久"
                        "谁离被进攻篮筐最近。默认 1.5s（在有比分牌真值的素材上"
                        "实测最准 6/8；改成 0 就是看进球瞬间，只有 4/8）")
    v.add_argument("--marks", default=None,
                   help="人工标点文件（scripts\\mark_landmarks.py 生成）："
                        "直接用你标的篮筐，跳过检测器 —— 换机位/检测器不灵时最稳")
    v.add_argument("--scoreboard-events", default=None,
                   help="外部记分牌事件 JSON（scripts/read_marked_scoreboard.py "
                        "的产出）—— 给模板匹配认不出的非标准台标用")
    v.add_argument("--basket-labels", default=None,
                   help="人工标注文件（scripts\\label_baskets.py 生成）："
                        "给了它就只报告标注为「进球」的时刻 —— 自动判据不可信时的准绳")
    v.add_argument("--hoop-sight-min-conf", type=float, default=None,
                   help="篮下判进球的置信度门槛（默认 0.5）。实测自动判据的误报"
                        "置信度可达 0.97，卡门槛救不了，请配合 --basket-labels 使用")
    v.add_argument("--basket-model", default="",
                   help="训练好的进球判定模型目录。默认**不加载**：实测当前模型"
                        "（正样本只有 8 个）会塌成常数输出、没有判别力；"
                        "等你标够球、重训并用留一法验证过之后再显式指定这个目录")
    v.add_argument("--ball-weights", default="",
                   help="训练好的球检测权重")
    v.add_argument("--no-auto-sliding", action="store_true",
                   help="没有可用标定时不自动做逐帧滑动标定（默认会做）")
    v.add_argument("--sliding-stride", type=int, default=5,
                   help="滑动标定的锚点间隔（帧）；越小越准越慢")
    v.add_argument("--sliding-max-seconds", type=float, default=0.0,
                   help="只对前 N 秒做滑动标定（0=整段）")
    v.add_argument("--hoop", default=None,
                   help="手动篮筐提示，格式 x,y 或 x,y,r（第一帧像素坐标）")
    _add_common(v)
    v.set_defaults(func=cmd_video)

    sv = sub.add_parser("serve", help="启动 Web 服务")
    sv.add_argument("--host", default="127.0.0.1")
    sv.add_argument("--port", type=int, default=8000)
    sv.add_argument("--reload", action="store_true")
    sv.set_defaults(func=cmd_serve)
    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
