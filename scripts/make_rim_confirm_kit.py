# -*- coding: utf-8 -*-
"""按指定时刻，从**原片**切出"带标注的画面 + 短片"，供人工目视核对。

为什么需要它：`raw_track.json` 只能告诉我们"当时筐框在哪、跟踪状态是什么"，
回答不了"那个框里到底是什么东西"、"这一球是不是真实投篮"。这两件事必须看画面。
而复核的人（用户）不该自己去对着时间码找帧，所以这里把画面连同标注一起做出来：

  * 每个时刻一张 **contact sheet**（8 张关键帧拼图）：绿框=当帧实际跟踪的筐，
    橙框=该帧的其它候选（含被拒原因），红框=上一次跟踪位置（重捕时刻对比用），
    黄框=球，蓝十字=人工提示点；
  * 每个时刻一段 **带标注的短片**（默认前 4 秒到后 3 秒，覆盖"判定收尾前的过程"）；
  * 一份 `README_怎么看.md`（每个时刻要回答什么问题 + 该时刻的产物事实）
    和 `confirm_table.csv`（待人工填写）。

画的框全部来自**同一次运行的逐帧依据**，不是重新推理；时间码与 trace 的 `t` 一致。

用法：
    python scripts/make_rim_confirm_kit.py --job out/<job> --video <原片> \
        --at 8.2 25.4 31.5 13.51 17.08
"""
from __future__ import annotations

import argparse
import csv
import json
from pathlib import Path

import cv2

ROOT = Path(__file__).resolve().parents[1]

GREEN = (60, 200, 60)      # 实际跟踪的筐
ORANGE = (0, 150, 255)     # 其它候选
RED = (60, 60, 230)        # 上一次跟踪位置
YELLOW = (0, 220, 255)     # 球
BLUE = (230, 120, 0)       # 人工提示点


def load_trace(job: Path) -> dict:
    data = json.loads((job / "raw_track.json").read_text(encoding="utf-8"))
    return (data.get("detections_meta") or {}).get("legacy_shots") or {}


def box_of(det) -> tuple | None:
    if not det:
        return None
    b = det.get("xyxy")
    return tuple(int(round(v)) for v in b) if b else None


def draw(frame, row: dict, hint, prev_state, label: str, trail: list | None = None):
    img = frame.copy()
    # 最近几秒的跟踪位置轨迹（把"逐帧走位"画成一条线，一眼能看出框是不是在滑走）
    if trail and len(trail) > 1:
        pts = [(int(x), int(y)) for x, y in trail]
        for a, b in zip(pts, pts[1:]):
            cv2.line(img, a, b, (0, 120, 0), 2)
    # 其它候选（细橙框 + 拒绝原因）
    for c in row.get("rim_candidates") or []:
        b = box_of(c)
        if not b or c.get("selection_reason") == "selected":
            continue
        cv2.rectangle(img, b[:2], b[2:], ORANGE, 2)
        cv2.putText(img, str(c.get("selection_reason"))[:12], (b[0], max(14, b[1] - 6)),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.5, ORANGE, 1, cv2.LINE_AA)
    # 送入跟踪器的框
    b = box_of(row.get("rim"))
    if b:
        cv2.rectangle(img, b[:2], b[2:], (200, 200, 200), 1)
    # 上一次跟踪位置（重捕时用来对比）
    if prev_state:
        px, py = int(prev_state["cx"]), int(prev_state["cy"])
        cv2.drawMarker(img, (px, py), RED, cv2.MARKER_CROSS, 28, 2)
        cv2.putText(img, "prev", (px + 8, py - 8), cv2.FONT_HERSHEY_SIMPLEX, 0.55, RED, 2,
                    cv2.LINE_AA)
    # 实际跟踪的筐：fresh=True 才画绿色实框；fresh=False 是"保留的历史位置"，
    # 用虚线灰框 + STALE 标注，避免被误读成"此刻有效的跟踪"。
    tr = row.get("tracked_rim")
    if tr:
        x1, y1 = int(tr["cx"] - tr["w"] / 2), int(tr["cy"] - tr["h"] / 2)
        x2, y2 = int(tr["cx"] + tr["w"] / 2), int(tr["cy"] + tr["h"] / 2)
        fresh = bool(tr.get("fresh", True))
        if fresh:
            cv2.rectangle(img, (x1, y1), (x2, y2), GREEN, 3)
            cv2.putText(img, "tracked_rim", (x1, max(16, y1 - 8)), cv2.FONT_HERSHEY_SIMPLEX,
                        0.6, GREEN, 2, cv2.LINE_AA)
        else:
            for a, b2 in (((x1, y1), (x2, y1)), ((x2, y1), (x2, y2)), ((x2, y2), (x1, y2)),
                          ((x1, y2), (x1, y1))):
                # 用短线段画虚线，表示"这是过期位置，不是当前跟踪"
                steps = max(2, int(((a[0] - b2[0]) ** 2 + (a[1] - b2[1]) ** 2) ** 0.5 / 18))
                for k in range(steps):
                    p = (int(a[0] + (b2[0] - a[0]) * k / steps), int(a[1] + (b2[1] - a[1]) * k / steps))
                    q = (int(a[0] + (b2[0] - a[0]) * (k + 0.5) / steps),
                         int(a[1] + (b2[1] - a[1]) * (k + 0.5) / steps))
                    cv2.line(img, p, q, (150, 150, 150), 2)
            cv2.putText(img, "STALE tracked_rim (last seen t=%.2f)"
                        % float(tr.get("last_seen_t") or 0),
                        (x1, max(16, y1 - 8)), cv2.FONT_HERSHEY_SIMPLEX, 0.55, (200, 200, 200), 2,
                        cv2.LINE_AA)
    # 球
    bb = box_of(row.get("ball"))
    if bb:
        cv2.rectangle(img, bb[:2], bb[2:], YELLOW, 2)
    # 提示点
    if hint:
        cv2.drawMarker(img, (int(hint[0]), int(hint[1])), BLUE, cv2.MARKER_TILTED_CROSS, 30, 3)
    cv2.rectangle(img, (0, 0), (img.shape[1], 34), (0, 0, 0), -1)
    cv2.putText(img, label, (10, 24), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (255, 255, 255), 2,
                cv2.LINE_AA)
    return img


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--job", required=True, help="out/<job_id>")
    ap.add_argument("--video", required=True, help="原片（与任务同一条）")
    ap.add_argument("--at", type=float, nargs="+", required=True, help="要核对的时间码（秒）")
    ap.add_argument("--pre", type=float, default=4.0, help="短片向前留几秒")
    ap.add_argument("--post", type=float, default=3.0, help="短片向后留几秒")
    ap.add_argument("--sheet", type=int, default=8, help="拼图取几张关键帧")
    ap.add_argument("--trail", type=float, default=3.0, help="画最近几秒的跟踪位置轨迹")
    ap.add_argument("--title", default="", help="核对包标题（默认「篮筐跟踪 · 画面核对包」）")
    # 核对包也用来核对"轨迹候选到底是不是真实出手"，那时默认的篮筐问题就不适用了。
    # 给了 --ask 就整套替换，避免 README 里写着与本次目的无关的问题。
    ap.add_argument("--ask", action="append", default=[],
                    help="要回答的问题（可重复给多条；给了就替换默认的篮筐核对问题）")
    ap.add_argument("--out", default="")
    args = ap.parse_args()

    job = Path(args.job)
    out = Path(args.out) if args.out else job / "confirm_rim"
    out.mkdir(parents=True, exist_ok=True)
    trace = load_trace(job)
    if not trace:
        print("这个任务没有 legacy_shots 逐帧依据")
        return 2
    frames = trace["frames"]
    rows = sorted(frames, key=lambda f: f["t"])
    hint = (trace.get("rim_tracking") or {}).get("center_hint")
    transitions = (trace.get("rim_tracking") or {}).get("transitions") or []
    events = trace.get("events") or []

    cap = cv2.VideoCapture(str(args.video))
    if not cap.isOpened():
        print("打不开视频：", args.video)
        return 2
    fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
    W = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    H = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    writer = None
    # 一次顺序解码整段视频，遇到要核对的时间码就取帧（避免反复 seek）
    wants = []
    for t in args.at:
        wants.append({"t": t, "lo": max(0.0, t - args.pre), "hi": t + args.post,
                      "sheet": [], "clip": [], "done": False})
    printable = []
    trail = []          # [(t, cx, cy)] 最近若干秒的跟踪位置
    i = 0
    while True:
        ok, frame = cap.read()
        if not ok:
            break
        t = i / fps
        row = min(rows, key=lambda r: abs(r["t"] - t))
        tr_now = row.get("tracked_rim")
        if tr_now:
            trail.append((t, tr_now["cx"], tr_now["cy"]))
        while trail and t - trail[0][0] > args.trail:
            trail.pop(0)
        for w in wants:
            if w["done"] or not (w["lo"] <= t <= w["hi"]):
                continue
            prev_state = None
            for tr in transitions:
                if abs(tr["t"] - w["t"]) < 0.6:
                    prev_state = tr.get("previous")
            # 顶部标签必须用**当前这一帧**的行内数据（此前误用了"目标时刻最近的一行"，
            # 导致标签坐标与画面里的绿框对不上）。
            cur_tr = row.get("tracked_rim") or {}
            label = ("t=%.2f  跟踪筐(%s,%s)%s  状态=%s  证据段=%s  第%d帧"
                     % (t,
                        ("%.0f" % cur_tr["cx"]) if cur_tr.get("cx") is not None else "-",
                        ("%.0f" % cur_tr["cy"]) if cur_tr.get("cy") is not None else "-",
                        "" if cur_tr.get("fresh", True) else "[过期]",
                        row.get("tracker_reason"), row.get("evidence_segment"), i))
            img = draw(frame, row, hint, prev_state, label,
                       [(x, y) for _tt, x, y in trail])
            w["clip"].append(img)
            # 拼图：按**时间顺序**收集（此前按"离目标时刻的绝对距离"排序，
            # 会把 t+2 和 t-2 的帧混在一起，让人误判动作先后）
            w["sheet"].append((t, img))
            if t >= w["hi"] - 1e-6:
                w["done"] = True
        i += 1
    cap.release()

    summary = []
    # 文件名带上**来源视频的全名**：用户实测反馈"只看到 t03p34 这种文件名不知道是哪条视频"，
    # 所以每个时刻的产物名里都写清视频，避免离开上下文就无法辨认。
    src_stem = Path(args.video).stem
    for w in wants:
        time_tag = ("t%05.2f" % w["t"]).replace(".", "p")
        tag = "%s_%s" % (src_stem, time_tag)
        # 短片
        if w["clip"]:
            h, wd = w["clip"][0].shape[:2]
            vw = cv2.VideoWriter(str(out / ("%s_clip.mp4" % tag)),
                                 cv2.VideoWriter_fourcc(*"mp4v"), fps, (wd, h))
            for f in w["clip"]:
                vw.write(f)
            vw.release()
        # 拼图
        sheet = [im for _d, im in sorted(w["sheet"], key=lambda x: x[0])]
        step = max(1, len(sheet) // args.sheet)
        picked = [t for t, _im in sorted(w["sheet"], key=lambda x: x[0])][::step][:args.sheet]
        sheet = sheet[::step][:args.sheet]
        if picked:
            (out / ("%s_tiles.txt" % tag)).write_text(
                "拼图按时间顺序排列（左→右、上→下），每格对应：\n"
                + "\n".join("  第%d格  t=%.2fs" % (k + 1, tt) for k, tt in enumerate(picked))
                + "\n", encoding="utf-8")
        if sheet:
            import numpy as np
            cols = 4
            rows_n = (len(sheet) + cols - 1) // cols
            hh, ww = sheet[0].shape[:2]
            canvas = np.zeros((rows_n * hh, cols * ww, 3), dtype=sheet[0].dtype)
            for k, im in enumerate(sheet):
                r, c = divmod(k, cols)
                canvas[r * hh:(r + 1) * hh, c * ww:(c + 1) * ww] = im
            cv2.imwrite(str(out / ("%s_sheet.jpg" % tag)), canvas,
                        [int(cv2.IMWRITE_JPEG_QUALITY), 88])
        near = min(rows, key=lambda r: abs(r["t"] - w["t"]))
        tr = near.get("tracked_rim") or {}
        ev = [e for e in events if abs(e["t"] - w["t"]) < 0.6]
        summary.append({
            "t": w["t"], "tag": tag,
            "tracker_reason": near.get("tracker_reason"),
            "segment": tr.get("segment"), "cx": round(tr.get("cx") or 0, 1),
            "cy": round(tr.get("cy") or 0, 1), "w": round(tr.get("w") or 0, 1),
            "h": round(tr.get("h") or 0, 1),
            "aspect": round((tr.get("w") or 1) / max(1.0, tr.get("h") or 1), 2),
            "n_candidates": len(near.get("rim_candidates") or []),
            "event": (ev[0]["type"] if ev else None),
            "clip_s": round(len(w["clip"]) / max(1.0, fps), 2),
        })
        print("   %s  跟踪筐=(%.0f,%.0f) 宽x高=%.0fx%.0f 长宽比=%.2f 状态=%s 候选=%d 事件=%s"
              % (tag, tr.get("cx") or 0, tr.get("cy") or 0, tr.get("w") or 0, tr.get("h") or 0,
                 (tr.get("w") or 1) / max(1.0, tr.get("h") or 1), near.get("tracker_reason"),
                 len(near.get("rim_candidates") or []), ev[0]["type"] if ev else "-"))

    first = next((f for f in rows if f.get("tracked_rim")), None)
    base_w = ((first or {}).get("tracked_rim") or {}).get("w") or 0

    # 跟踪位置轨迹（每 0.5s 一点），给 README 当对照表
    traj = []
    for f in rows:
        tr = f.get("tracked_rim")
        if tr and abs(f["t"] * 2 - round(f["t"] * 2)) < 0.02:
            traj.append((f["t"], tr["cx"], tr["cy"]))
    traj_lines = []
    last_cx = None
    for t, cx, cy in traj:
        if last_cx is None or abs(cx - last_cx) >= 8:
            traj_lines.append("| %.2f | %.0f | %.0f |" % (t, cx, cy))
            last_cx = cx

    ask = args.ask or [
        "远处重捕时刻（8.2 / 25.4 / 31.5 秒）：绿框里是**篮筐**吗？还是记分牌/广告牌/观众/别的什么？",
        "跟踪走位段（9.8 / 11.3 / 12.8 秒）：绿框是**沿着什么东西滑过去**的？"
        "（这段轨迹显示筐位在 4 秒里从 x≈1841 平滑滑到 x≈1048）",
        "13.51 / 17.08 秒（判定收尾时刻，**请连同前几秒一起看**）：这两条是不是**真实投篮**？"
        "球有没有离手、有没有朝筐飞行？",
    ]
    ask_md = "\n".join("%d. %s" % (i + 1, q) for i, q in enumerate(ask))

    (out / "README_怎么看.md").write_text(
        "# %s\n\n" % (args.title or "篮筐跟踪 · 画面核对包") +
        "**来源视频**：`%s`（任务目录 `%s`）\n\n" % (args.video, job) +
        "每个时刻的产物文件名都以视频全名为前缀（例如 `%s_%s_sheet.jpg`），"
        "离开这份 README 也能认出是哪条视频。\n\n" % (src_stem, ("t%05.2f" % wants[0]["t"]).replace(".", "p") if wants else "t00p00") +
        "这些画面来自**同一次运行的逐帧依据**（不是重新推理），时间码与 trace 的 `t` 一致。\n\n"
        "**框的画法**：**绿色实框 = 当帧有效跟踪的筐**（`tracked_rim` 且 `fresh=true`）；\n"
        "**灰色虚线框 = 过期的历史位置**（`fresh=false`，框上标 `STALE` 与最后观测时刻）——\n"
        "它不是当前跟踪目标，不要当成『此刻框住了什么』；灰细框 = 送进跟踪器的框；\n"
        "橙框 = 该帧的其它候选（附拒绝原因）；红叉 = 上一次跟踪位置（重捕时刻用来对比）；\n"
        "黄框 = 球；蓝叉 = 人工提示点；**深绿折线 = 最近 %.0f 秒跟踪筐中心的轨迹**。\n\n" % args.trail +
        "每个时刻有两个文件：`*_sheet.jpg`（8 张关键帧拼图，**按时间顺序排列**：\n"
        "左上→右下即时间先后）和 `*_clip.mp4`（前后几秒的带标注短片，看过程）。\n"
        "顶部标签取的是**该帧自己的**跟踪筐坐标，与画面里的框一致。\n\n"
        "## 要回答的问题\n\n" + ask_md + "\n\n"
        "把结论填进 `confirm_table.csv` 的 `我看到的` / `结论` 两列即可。\n\n"
        "## 每个时刻的产物事实（来自 trace，供对照）\n\n"
        "| 时刻 | 跟踪状态 | 实际筐 cx,cy | 宽x高 | 长宽比 | 与首次确认筐宽之比 | 该帧候选数 | 引擎事件 |\n"
        "|---|---|---|---|---|---|---|---|\n"
        + "\n".join(
            "| %.2fs | %s | %.0f, %.0f | %.0fx%.0f | %.2f | %s | %d | %s |"
            % (s["t"], s["tracker_reason"], s["cx"], s["cy"], s["w"], s["h"], s["aspect"],
               ("%.2f" % (s["w"] / base_w)) if base_w else "-", s["n_candidates"],
               s["event"] or "（无）")
            for s in summary)
        + "\n\n首次确认的筐宽 = %.1fpx（作为尺度参照）。\n\n" % base_w
        + "## 跟踪筐位置轨迹（每 0.5s，位置变化 ≥8px 才列一行）\n\n"
          "| t(s) | 筐 cx | 筐 cy |\n|---|---|---|\n" + "\n".join(traj_lines) + "\n\n"
          "> 注意：轨迹里长时间单向平滑移动（每帧几十像素）说明跟踪框在**走**，"
          "而不是在原地抖动；跳变闸门对『每步都小于上限』的走位不起作用。\n",
        encoding="utf-8")

    with (out / "confirm_table.csv").open("w", encoding="utf-8-sig", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["时刻(s)", "跟踪状态", "实际跟踪筐(x,y)", "框宽x高", "长宽比",
                    "该帧候选数", "引擎事件", "我看到的（请填）", "结论（请填）"])
        for s in summary:
            w.writerow([s["t"], s["tracker_reason"], "%s,%s" % (s["cx"], s["cy"]),
                        "%sx%s" % (s["w"], s["h"]), s["aspect"], s["n_candidates"],
                        s["event"] or "", "", ""])
    print("\n核对包：%s" % out)
    print("  怎么看：%s" % (out / "README_怎么看.md"))
    print("  填表  ：%s" % (out / "confirm_table.csv"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
