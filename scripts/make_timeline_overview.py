# -*- coding: utf-8 -*-
"""整片时间轴总览图：把一条视频按固定间隔抽帧拼成一张总览图，并标出各来源的出手时刻。

为什么需要它：用户实测反馈"只看某个时刻的拼图，不知道它在整条视频里的位置，
也分不清哪次出手是哪一次"。这张图给的是**全局定位**：
每个格子一分钟内的一帧 + 该秒的时间码 + 落在这一秒里的
「真值 / 引擎事件 / 候选（含未对应事件）」标记，用来把"第几次出手"对上具体秒数。

用法::

    python -B scripts/make_timeline_overview.py --video <原片> --job out/<job_id>
    # 只画视频、不带任何标记也可以：
    python -B scripts/make_timeline_overview.py --video <原片>

产物：``<out>/timeline_overview.jpg``（总览图）与 ``<out>/timeline_overview.csv``（逐格时刻与标记）。
"""
import argparse
import csv
import json
from pathlib import Path

import cv2
import numpy as np

GREEN = (60, 200, 60)      # 真值
BLUE = (230, 120, 0)       # 引擎事件
ORANGE = (0, 165, 255)     # 候选（已与事件对应）
RED = (60, 60, 230)        # 候选（未对应任何事件）


def load_marks(job: Path) -> tuple[list, list, list]:
    """从任务产物里取三类标记：候选（分是否对应事件）、引擎事件。真值由调用方另外给。

    候选**现场用当前代码重算**（`shot_candidates`），而不是读产物里存的那一份：
    产物可能是旧代码生成的（实测踩到：旧的 day 产物里 10.177 还挂在主列表，
    新代码里它属于"证据跨段"）。重算失败时退回存的那一份，并明说来源。
    """
    trace = (json.loads((job / "raw_track.json").read_text(encoding="utf-8"))
             .get("detections_meta") or {}).get("legacy_shots") or {}
    events = [(float(e["t"]), e.get("type") or "?") for e in (trace.get("events") or [])]
    sc = None
    try:
        import sys
        root = Path(__file__).resolve().parents[1]
        if str(root / "src") not in sys.path:
            sys.path.insert(0, str(root / "src"))
        from aihoop.legacy_shots.stream import shot_candidates as _sc
        sc = _sc(trace.get("frames"), trace.get("config"), trace.get("events"))
        src = "现场重算"
    except Exception as exc:                      # 依赖缺失/旧 trace 也不该让工具挂掉
        print("[warn] 候选现场重算失败（%s），改用产物里存的那一份" % exc)
        src = "产物存值"
    if sc is None:
        sc = trace.get("shot_candidates") or {}
    print("[info] 候选来源：%s（主列表 %s 条，跨段 %s 条）"
          % (src, sc.get("count"), sc.get("cross_segment_count", 0)))
    matched = [(float(c["release_t"]), c.get("kind") or "?") for c in (sc.get("candidates") or [])
               if c.get("matched")]
    unmatched = [(float(c["release_t"]), c.get("kind") or "?") for c in (sc.get("candidates") or [])
                 if not c.get("matched")]
    for c in (sc.get("cross_segment") or []):
        unmatched.append((float(c["release_t"]), "跨段"))
    return events, matched, unmatched


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--video", required=True)
    ap.add_argument("--job", default="", help="out/<job_id>（给了就画事件与候选标记）")
    ap.add_argument("--truth", default="", help="真值 CSV（可选，需要 release_s 与 result 两列）")
    ap.add_argument("--step", type=float, default=1.0, help="每格间隔秒数")
    ap.add_argument("--cols", type=int, default=8)
    ap.add_argument("--tile-w", type=int, default=320)
    ap.add_argument("--out", default="")
    args = ap.parse_args()

    video = Path(args.video)
    out = Path(args.out) if args.out else ((Path(args.job) / "timeline_overview")
                                           if args.job else video.with_suffix(""))
    out.mkdir(parents=True, exist_ok=True)

    events, matched, unmatched = load_marks(Path(args.job)) if args.job else ([], [], [])
    truth = []
    if args.truth and Path(args.truth).exists():
        with open(args.truth, encoding="utf-8-sig", newline="") as fh:
            for row in csv.DictReader(fh):
                try:
                    truth.append((float(row.get("release_s") or 0), row.get("result") or "?"))
                except (TypeError, ValueError):
                    continue

    cap = cv2.VideoCapture(str(video))
    if not cap.isOpened():
        print("打不开视频：", video)
        return 2
    fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
    total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
    duration = total / fps if fps else 0.0

    tiles = []
    rows = []
    t = 0.0
    while t <= duration:
        cap.set(cv2.CAP_PROP_POS_FRAMES, int(round(t * fps)))
        ok, frame = cap.read()
        if not ok:
            break
        h, w = frame.shape[:2]
        scale = args.tile_w / float(w)
        tile = cv2.resize(frame, (args.tile_w, max(1, int(round(h * scale)))))
        # 该秒内的标记（取 [t, t+step) 区间）
        def pick(items):
            return [f"{v:.1f}{k}" for v, k in items if t <= v < t + args.step]

        lines = []
        if pick(truth):
            lines.append(("真值 " + " ".join(pick(truth)), GREEN))
        if pick(events):
            lines.append(("事件 " + " ".join(pick(events)), BLUE))
        if pick(matched):
            lines.append(("候选 " + " ".join(pick(matched)), ORANGE))
        if pick(unmatched):
            lines.append(("未对应 " + " ".join(pick(unmatched)), RED))
        rows.append(dict(t=round(t, 2), truth=";".join(pick(truth)), events=";".join(pick(events)),
                         candidates_matched=";".join(pick(matched)),
                         candidates_unmatched=";".join(pick(unmatched))))

        bar_h = 20 + 18 * len(lines)
        cv2.rectangle(tile, (0, 0), (tile.shape[1], bar_h + 8), (0, 0, 0), -1)
        cv2.putText(tile, "t=%.1fs" % t, (6, 18), cv2.FONT_HERSHEY_SIMPLEX, .55,
                    (255, 255, 255), 2, cv2.LINE_AA)
        for i, (text, color) in enumerate(lines):
            cv2.putText(tile, text[:38], (6, 36 + 18 * i), cv2.FONT_HERSHEY_SIMPLEX, .42,
                        color, 1, cv2.LINE_AA)
        tiles.append(tile)
        t += args.step
    cap.release()
    if not tiles:
        print("没抽到帧")
        return 2

    cols = max(1, args.cols)
    rown = (len(tiles) + cols - 1) // cols
    th, tw = tiles[0].shape[:2]
    canvas = np.zeros((rown * th, cols * tw, 3), dtype=tiles[0].dtype)
    for k, tile in enumerate(tiles):
        r, c = divmod(k, cols)
        canvas[r * th:(r + 1) * th, c * tw:(c + 1) * tw] = tile

    img_path = out / "timeline_overview.jpg"
    cv2.imwrite(str(img_path), canvas, [int(cv2.IMWRITE_JPEG_QUALITY), 86])
    with (out / "timeline_overview.csv").open("w", encoding="utf-8-sig", newline="") as fh:
        wr = csv.DictWriter(fh, fieldnames=["t", "truth", "events", "candidates_matched",
                                            "candidates_unmatched"])
        wr.writeheader()
        wr.writerows(rows)
    print("总览图：%s（%d 格 × %s，每格 %.1fs）" % (img_path, len(tiles), "%dx%d" % (tw, th), args.step))
    print("逐格表：%s" % (out / "timeline_overview.csv"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
