# -*- coding: utf-8 -*-
"""在**有人工时间戳**的两段素材上做定时比对（±2s 容差，按旧项目的标注口径）。

为什么单独做：这两段（DVIDS 白天/夜间室外，公有领域）是仓库里唯一带
`release_s / rim_s / result` 人工真值的素材，正好补上"独立测试集"的一部分。
判定口径沿用 `数球任务说明.md`：
  * 命中判据 —— 出手时间在真值 release_s ±2s 内，或穿筐时间在 rim_s ±2s 内；
  * 真值 result=unknown 的球不计入分母；
  * 输出"匹配到的进球 / 真值已知进球"与"多报的出手"，不做任何美化。

用法：python eval_timed_truth.py
"""
from __future__ import annotations

import csv
import json
import sys
from pathlib import Path

sys.path.insert(0, r"D:\dsh_folder\aihoop\src")

from aihoop.court import Calibration  # noqa: E402
from aihoop.sources import VideoSource  # noqa: E402

ROOT = Path(r"D:\dsh_folder\aihoop")
SAMPLES = Path(r"D:\dsh_folder\aihoop_assets\samples")
TRUTH = ROOT / "eval" / "ground_truth"
BALL = ROOT / "runs" / "detect" / "ball" / "weights" / "best.pt"
RIM = ROOT / "runs" / "detect" / "rim" / "weights" / "best.pt"
TOL = 2.0

CLIPS = ["day_outdoor_masumghar_32s.mp4", "night_outdoor_arifjan_32s.mp4"]


def load_truth(name: str) -> list[dict]:
    p = TRUTH / (Path(name).stem + ".csv")
    rows = []
    if not p.exists():
        return rows
    with p.open(encoding="utf-8") as fh:
        for r in csv.DictReader(fh):
            if not (r.get("shot_no") or "").strip():
                continue
            rows.append({
                "no": int(r["shot_no"]),
                "release_s": float(r["release_s"]) if (r.get("release_s") or "").strip() else None,
                "rim_s": float(r["rim_s"]) if (r.get("rim_s") or "").strip() else None,
                "result": (r.get("result") or "unknown").strip(),
                "note": (r.get("note") or "")[:60],
            })
    return rows


def run(name: str, hoop_weights: str) -> dict:
    video = SAMPLES / name
    cal = Calibration(name="unavailable", method="unavailable")
    src = VideoSource(str(video), cal, stride=1, player_stride=2,
                      detect_players=False, score_policy="auto",
                      visual_shot_value=2, auto_sliding=False,
                      ball_weights=str(BALL), hoop_weights=(hoop_weights or ""),
                      device="0")
    rt = src.run(progress=lambda p, m="": None)
    meta = rt.detections_meta
    return {
        "attempts": [{"t": round(a.t, 2), "made": a.made,
                      "evidence": getattr(a, "evidence", ""),
                      "crossing_t": getattr(a, "crossing_t", None),
                      "source": a.source} for a in rt.attempts],
        "visual": meta.get("visual"),
        "visual_error": meta.get("visual_error"),
        "hoopsight_skipped": meta.get("hoopsight_skipped"),
        "hoop_drift": (meta.get("hoop") or {}).get("robust_drift_px"),
        "camera": (meta.get("camera_motion") or {}).get("verdict"),
    }


def main() -> int:
    out = []
    # 两种篮筐路径都跑：product = 与线上一致（用训练好的 rim 权重）；
    # heuristic = 不用权重时的颜色/形状启发式（保留对照，便于看差异从哪来）。
    modes = [("product(rim权重)", str(RIM) if RIM.exists() else ""),
             ("heuristic(无权重)", "")]
    for name in CLIPS:
        truth = load_truth(name)
        print(f"\n{'=' * 92}\n{name}   人工真值 {len(truth)} 球"
              f"（已知进球 {sum(1 for t in truth if t['result'] == 'make')}）")
        for t in truth:
            print(f"    真值 #{t['no']}: 出手 {t['release_s']}s 到筐 {t['rim_s']}s "
                  f"→ {t['result']}   {t['note']}")
        for tag, hw in modes:
            try:
                r = run(name, hw)
            except Exception as e:  # noqa: BLE001
                print(f"    [{tag}] 运行失败：{type(e).__name__}: {e}")
                out.append({"video": name, "mode": tag, "truth": truth,
                            "error": str(e)[:200]})
                continue
            report(name, tag, truth, r)
            out.append({"video": name, "mode": tag, "truth": truth, "model": r})
    dest = ROOT / "out" / "eval_timed_truth.json"
    dest.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n明细：{dest}")
    return 0


def report(name: str, tag: str, truth: list[dict], r: dict) -> None:
    atts = r["attempts"]
    print(f"\n  [{tag}] 模型：{len(atts)} 次出手"
          f"（进 {sum(1 for a in atts if a['made'])} / 未知 "
          f"{sum(1 for a in atts if a['made'] is None)}）；"
          f"镜头={r['camera']} 篮筐稳健漂移={r['hoop_drift']}")
    for a in atts:
        print(f"      t={a['t']:>6.2f} made={str(a['made']):>5} "
              f"crossing_t={a['crossing_t']} evidence={a['evidence'] or '-'}")
    for k in ("visual_error", "hoopsight_skipped"):
        if r.get(k):
            print(f"      {k}: {str(r[k])[:130]}")
    matched, unmatched_truth, used = [], [], set()
    for t in truth:
        hit = None
        for i, a in enumerate(atts):
            if i in used:
                continue
            near_release = (t["release_s"] is not None
                            and abs(a["t"] - t["release_s"]) <= TOL)
            near_rim = (t["rim_s"] is not None and a["crossing_t"] is not None
                        and abs(a["crossing_t"] - t["rim_s"]) <= TOL)
            if near_release or near_rim:
                hit = (i, "出手匹配" if near_release else "穿筐匹配")
                break
        if hit:
            used.add(hit[0])
            matched.append((t, atts[hit[0]], hit[1]))
        else:
            unmatched_truth.append(t)
    extra = [a for i, a in enumerate(atts) if i not in used]
    for t, a, how in matched:
        ok = "进" if a["made"] else ("未中" if a["made"] is False else "未知")
        agree = ("一致" if (t["result"] == "make") == bool(a["made"])
                 else ("模型未知" if a["made"] is None else "**不一致**"))
        print(f"      真值#{t['no']}({t['result']}) ↔ 模型 t={a['t']} ({ok}) [{how}] {agree}")
    for t in unmatched_truth:
        print(f"      **漏检**：真值#{t['no']} 出手 {t['release_s']}s → {t['result']}")
    for a in extra:
        print(f"      多报：模型 t={a['t']} made={a['made']}")
    known_makes = [t for t in truth if t["result"] == "make"]
    hit_makes = [t for t, _a, _h in matched if t["result"] == "make"]
    print(f"      小结：真值已知进球 {len(known_makes)} 个，模型对上 {len(hit_makes)} 个；"
          f"漏检 {len(unmatched_truth)} 条；多报 {len(extra)} 次")


if __name__ == "__main__":
    raise SystemExit(main())
