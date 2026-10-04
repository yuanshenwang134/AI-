# -*- coding: utf-8 -*-
"""中心锁定半径/提示点的**离线敏感性扫描**（不占 GPU，不重跑推理）。

为什么需要它：运行时 A/B（`scripts/center_lock_ab.py`）一次 1080p 70 秒素材要 ~35 分钟，
而"锁半径取多少、提示点该点在哪个筐上"是要扫的二维参数。这里用**同一次运行**已经录好的
逐帧依据（`raw_track.json → detections_meta.legacy_shots`）离线重放，把两条轴一起扫完。

**必须知道的局限（不要过度解读）**：
  * 录下来的 `frames[].rim` 只是"引擎当时选中的那个筐"，**不是该帧的全部候选**。
    所以离线回放里的中心锁定只能"否掉这一帧的筐"，不能"改选另一个候选"。
    也就是说：它回答的是「如果禁止远离提示点的筐，判定会变成什么样」，
    不能回答「候选选择器会不会改选到正确的筐」。
  * 要验证"候选改选"必须带插桩重跑推理（GPU）。
  * **与产品代码的三处口径偏差**（对着 `legacy_shots/stream.py` 核过）：
      1) 切镜（`cut`）时本脚本清空锁宽重算，产品代码的 `reset()` **保留** `center_lock_width`；
      2) 半径基准的筐宽：本脚本用**原始检测框宽**，产品用**跟踪框宽** `engine.rim.w`；
      3) 本脚本报的是**帧数**，产品的 `center_rejected` 是**候选数**（逐候选累加）。
    有切镜的素材必须先对齐第 1 条，否则两边不可比。

用法：python scripts/center_lock_sweep.py <raw_track.json> [--out 结果.json]
"""
from __future__ import annotations

import argparse
import json
import math
import statistics
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from aihoop.legacy_shots.scorer import ScoreEngine  # noqa: E402
from aihoop.legacy_shots.stream import unpack  # noqa: E402


def replay(trace: dict, hint, widths: float) -> dict:
    """按 stream.py:82-90 的规则重放：离提示点超过 widths×锁宽 的筐一律弃用。"""
    cfg = json.loads(json.dumps(trace["config"]))
    cfg.setdefault("detect", {})["center_lock_widths"] = float(widths)
    fps = trace["fps"]
    engine = ScoreEngine(cfg, fps)
    events: list[dict] = []
    lock_w = None
    rejected = 0
    for row in trace["frames"]:
        if row.get("cut"):
            events.extend(engine.finalize(row["frame"], row["t"]))
            engine = ScoreEngine(cfg, fps)
            lock_w = None
        rim = unpack(row["rim"])
        if hint is not None and widths > 0 and rim is not None:
            radius = widths * (lock_w or (rim.xyxy[2] - rim.xyxy[0]))
            if math.hypot(rim.cx - hint[0], rim.cy - hint[1]) > radius:
                rim = None
                rejected += 1
        events.extend(engine.update(row["frame"], row["t"], unpack(row["ball"]), rim,
                                    [unpack(p) for p in row["persons"]]))
        if lock_w is None and engine.rim.initialized:
            lock_w = engine.rim.w
    if trace["frames"]:
        row = trace["frames"][-1]
        events.extend(engine.finalize(row["frame"] + 1, row["t"] + 1 / fps))
    return {"events": events, "rejected": rejected, "lock_w": lock_w}


def rim_x_at(trace: dict, events: list[dict]) -> list:
    frames = {int(f["frame"]): f for f in trace["frames"]}
    out = []
    for ev in events:
        f = frames.get(int(ev.get("frame") or -1)) or {}
        box = (f.get("rim") or {}).get("xyxy")
        out.append(round((box[0] + box[2]) / 2, 1) if box else None)
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("raw")
    ap.add_argument("--out", default="")
    args = ap.parse_args()
    raw = Path(args.raw)
    data = json.loads(raw.read_text(encoding="utf-8"))
    trace = (data.get("detections_meta") or {}).get("legacy_shots")
    if not trace:
        print("这个任务没有 legacy_shots 逐帧依据")
        return 2

    # 从被判罚用过的筐里挑出两个簇心当提示点候选（近筐/远筐），再加一个"两簇中点"做对照
    boxes = []
    frames = {int(f["frame"]): f for f in trace["frames"]}
    for ev in trace["events"]:
        box = ((frames.get(int(ev.get("frame") or -1)) or {}).get("rim") or {}).get("xyxy")
        if box:
            boxes.append(((box[0] + box[2]) / 2, (box[1] + box[3]) / 2))
    if not boxes:
        print("没有任何判罚用到筐，无法扫描")
        return 2
    xs = sorted(b[0] for b in boxes)
    lo = [(x, y) for x, y in boxes if x <= statistics.median(xs)]
    hi = [(x, y) for x, y in boxes if x > statistics.median(xs)]
    hints = []
    if lo:
        hints.append(("判罚筐左簇", (round(statistics.median([p[0] for p in lo]), 1),
                                    round(statistics.median([p[1] for p in lo]), 1))))
    if hi:
        hints.append(("判罚筐右簇", (round(statistics.median([p[0] for p in hi]), 1),
                                    round(statistics.median([p[1] for p in hi]), 1))))
    if lo and hi:
        hints.append(("两簇中点", (round((hints[0][1][0] + hints[1][1][0]) / 2, 1),
                                 round((hints[0][1][1] + hints[1][1][1]) / 2, 1))))

    out = {"raw": str(raw), "hints": [], "widths": [0.0, 1.5, 2.0, 3.0, 4.0]}
    print("逐帧依据：%d 帧；判罚用过的筐 %d 个" % (len(trace["frames"]), len(boxes)))
    for name, hint in hints + [("无提示(现状)", None)]:
        arm = {"hint_name": name, "hint": list(hint) if hint else None, "rows": []}
        print("\n提示点 %s = %s" % (name, hint))
        for w in out["widths"]:
            r = replay(trace, hint, w)
            types = [e["type"] for e in r["events"]]
            cxs = [c for c in rim_x_at(trace, r["events"]) if c is not None]
            span = round(max(cxs) - min(cxs)) if cxs else None
            arm["rows"].append({
                "widths": w, "events": len(r["events"]), "make": types.count("make"),
                "miss": types.count("miss"), "unknown": types.count("uncertain"),
                "rejected_frames": r["rejected"], "lock_w": r["lock_w"],
                "rim_x_span": span, "rim_x": cxs,
                "detail": [{"t": e["t"], "type": e["type"]} for e in r["events"]]})
            print("   widths=%-4s 出手=%-2d 进=%-2d 未中=%-2d 未知=%-2d 弃筐帧=%-5d "
                  "筐心跨度=%-6s 逐条筐心x=%s"
                  % (w, len(r["events"]), types.count("make"), types.count("miss"),
                     types.count("uncertain"), r["rejected"],
                     ("%spx" % span) if span is not None else "-", cxs))
        out["hints"].append(arm)

    dest = Path(args.out) if args.out else raw.parent / "center_lock_sweep.json"
    dest.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    print("\n明细：%s" % dest)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
