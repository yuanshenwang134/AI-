#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""为「人工确认这些出手到底进没进」生成一套看片材料（不需要 GPU、不需要后端）。

为什么需要它：判定结果为"未知"的球，最后只能靠人眼在**筐口那一两秒**做判断。
整段素材丢给人看没用 —— 一次出手几秒钟，筐口的关键动作只有 0.5 秒左右，
而且球常常正好在筐口被网/筐挡住（实测这两球各断了 0.43 / 0.44 秒，13 帧）。
所以这里把关键区间切成四种材料：

  1. `shot<ID>_rim_slow4x.mp4`        筐口附近整段，4 倍慢放（全画面）
  2. `shot<ID>_rim_zoom_slow4x.mp4`   同一段按检测到的筐心裁 ±170px，放大 2 倍
  3. `shot<ID>_rim_tight_slow6x.mp4`  **关键 0.6 秒**、筐心 ±80px、放大 4 倍、6 倍慢放 ← 主看这个
  4. `shot<ID>_key_sheet_NN.jpg`      **关键 0.6 秒逐帧**（一帧一格、带时间戳与帧号）
     `shot<ID>_key_zoom_sheet_NN.jpg` 同上但用第 3 项那种紧裁剪
     `shot<ID>_sheet_NN.jpg`          整个窗口的逐帧概览（粗一点，用来看过程）

以及两样"该记在哪"的东西：`confirm_table.csv`（待填）与 `README_怎么看.md`。

**文件名一律 ASCII**：ffmpeg 在 Windows 上对非 ASCII 路径会按本地代码页解释（实测踩过），
中文说明统一写在 README 里。

用法：

    python scripts/make_shot_confirm_kit.py \
        --video <nathan_freethrow.mov> --hoop-json out/<job>/raw_track.json \
        --shot 004:9.6:12.2 --shot 006:19.9:22.6 \
        --key  004:10.45:11.30 --key 006:20.85:21.70 \
        --out out/nathan_confirm

`--key` 是"最要紧的那 0.6~0.9 秒"（球到达筐面前后），用来看逐帧；可以不传，
不传时用窗口正中间截同样长的一段。
"""
from __future__ import annotations

import argparse
import csv
import json
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

from aihoop.highlight import ffmpeg_path                              # noqa: E402


def _cv2_np():
    import cv2
    import numpy as np
    return cv2, np


def load_hoop_samples(hoop_json: Path):
    meta = json.loads(hoop_json.read_text(encoding="utf-8"))
    h = (meta.get("detections_meta") or {}).get("hoop") or meta.get("hoop") or {}
    return h, [(float(s[0]), s[1]) for s in (h.get("samples") or [])]


def hoop_at(samples, t):
    if not samples:
        return None
    return min(samples, key=lambda s: abs(s[0] - t))[1]


def annotate(frame, t, fps, label=""):
    cv2, _ = _cv2_np()
    img = frame.copy()
    h, w = img.shape[:2]
    txt = f"t={t:6.2f}s f={int(round(t * fps)):4d} {label}"
    cv2.rectangle(img, (4, 4), (min(w - 4, 12 + 11 * len(txt)), 24), (0, 0, 0), -1)
    cv2.putText(img, txt, (8, 20), cv2.FONT_HERSHEY_SIMPLEX, 0.5,
                (255, 255, 255), 1, cv2.LINE_AA)
    return img


def write_video(frames_dir: Path, out_mp4: Path, fps: float, slow: int,
                start_number: int):
    """PNG 序列 → H.264 慢放视频（setpts 只改时间戳，输出帧率不变，播放器补帧）。

    **必须给 -start_number**：ffmpeg 的 image2 解复用器默认从 0 开始找帧，
    而我们的帧号是原始帧号（如 288 起），不给就报
    "Could find no file with path 'f%05d.png' and index in the range 0-4"（实测踩过）。
    """
    ff = ffmpeg_path()
    if not ff:
        return False, "没有 ffmpeg"
    cmd = [ff, "-y", "-loglevel", "error",
           "-framerate", f"{fps:.10f}", "-start_number", str(start_number),
           "-i", str(frames_dir / "f%05d.png"),
           "-vf", f"setpts={slow}*PTS", "-r", f"{fps:.10f}",
           "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
           "-pix_fmt", "yuv420p", "-movflags", "+faststart", str(out_mp4)]
    p = subprocess.run(cmd, capture_output=True, text=True, errors="replace")
    if p.returncode != 0:
        return False, (p.stderr or "")[-300:]
    return True, ""


def write_sheets(paths, labels, out_prefix: Path, title: str, chunk: int,
                 tile, cols: int):
    """按 chunk 帧一组输出拼图，返回 [(文件名, 起始标签, 结束标签)]。

    不输出一整张巨大的图：一屏放得下才看得清球和网。
    """
    cv2, np = _cv2_np()
    tw, th = tile
    pad, top = 6, 32
    made = []
    for i in range(0, len(paths), chunk):
        group = paths[i:i + chunk]
        gl = labels[i:i + chunk]
        rows = (len(group) + cols - 1) // cols
        sheet = np.full((top + rows * (th + pad) + pad, cols * (tw + pad) + pad, 3),
                        245, np.uint8)
        cv2.putText(sheet, f"{title}   {gl[0]}~{gl[-1]}", (10, 22),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.55, (20, 20, 20), 1, cv2.LINE_AA)
        for k, (p, lab) in enumerate(zip(group, gl)):
            img = cv2.imread(str(p))
            if img is None:
                continue
            if (img.shape[1], img.shape[0]) != (tw, th):
                img = cv2.resize(img, (tw, th),
                                 interpolation=(cv2.INTER_AREA if img.shape[1] > tw
                                                else cv2.INTER_CUBIC))
            cv2.putText(img, lab, (6, th - 8), cv2.FONT_HERSHEY_SIMPLEX, 0.55,
                        (0, 0, 0), 3, cv2.LINE_AA)
            cv2.putText(img, lab, (6, th - 8), cv2.FONT_HERSHEY_SIMPLEX, 0.55,
                        (255, 255, 255), 1, cv2.LINE_AA)
            r, c = divmod(k, cols)
            y = top + r * (th + pad)
            x = pad + c * (tw + pad)
            sheet[y:y + th, x:x + tw] = img
        out_jpg = out_prefix.parent / (out_prefix.name + f"_{i // chunk + 1:02d}.jpg")
        cv2.imwrite(str(out_jpg), sheet, [int(cv2.IMWRITE_JPEG_QUALITY), 90])
        made.append((out_jpg.name, gl[0], gl[-1]))
    return made


def crop_frames(cv2, video, f0, f1, fps, box, scale, out_dir: Path, hoop_of):
    """读 [f0,f1] 帧，按 box 裁剪+放大，并把时间戳画在**放大后**的图上。"""
    cap = cv2.VideoCapture(str(video))
    cap.set(cv2.CAP_PROP_POS_FRAMES, f0)
    x0, y0, cw, ch = box
    paths, labels = [], []
    for f in range(f0, f1 + 1):
        ok, frame = cap.read()
        if not ok:
            break
        t = f / fps
        img = frame[y0:y0 + ch, x0:x0 + cw]
        img = cv2.resize(img, (cw * scale, ch * scale), interpolation=cv2.INTER_CUBIC)
        h = hoop_of(t)
        if h is not None:
            cx = int((h["cx"] - x0) * scale); cy = int((h["cy"] - y0) * scale)
            rx = int(h["rx"] * scale)
            cv2.ellipse(img, (cx, cy), (rx, max(3, int(rx * 0.3))), 0, 0, 360,
                        (60, 240, 60), 1)
            cv2.line(img, (cx, cy - 5), (cx, cy + 5), (60, 240, 60), 1)
        txt = f"t={t:6.2f}s f={f:4d}"
        cv2.rectangle(img, (4, 4), (12 + 11 * len(txt), 26), (0, 0, 0), -1)
        cv2.putText(img, txt, (8, 21), cv2.FONT_HERSHEY_SIMPLEX, 0.5,
                    (255, 255, 255), 1, cv2.LINE_AA)
        cv2.imwrite(str(out_dir / f"f{f:05d}.png"), img,
                    [int(cv2.IMWRITE_PNG_COMPRESSION), 3])
        paths.append(out_dir / f"f{f:05d}.png")
        labels.append(f"{t:6.2f}s")
    cap.release()
    return paths, labels


def make_pip_frames(cv2, video, f0, f1, fps, box, scale, out_dir: Path, hoop_of):
    """左：全画面；右：筐口放大 —— 拼成一张，**一屏里既看得到整段动作又看得清筐口**。

    为什么要有这个：紧裁剪那种视频只框了筐口 160×140 像素（约占整帧 5%），
    画面其余部分是**故意裁掉**的，看的人会说"其他的我没看到"（实测踩过）。
    所以再出一版并排的：左边全画面（并在上面画出裁剪框），右边同一帧的筐口放大 4 倍。
    """
    cv2, np = _cv2_np()
    cap = cv2.VideoCapture(str(video))
    cap.set(cv2.CAP_PROP_POS_FRAMES, f0)
    x0, y0, cw, ch = box
    W = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH)); H = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    iw, ih = cw * scale, ch * scale
    gap, pad = 12, 8
    canvas_h = max(H, ih) + 2 * pad
    canvas_w = W + gap + iw + 2 * pad
    paths, labels = [], []
    for f in range(f0, f1 + 1):
        ok, frame = cap.read()
        if not ok:
            break
        t = f / fps
        left = annotate(frame, t, fps)
        cv2.rectangle(left, (x0, y0), (x0 + cw, y0 + ch), (0, 220, 255), 2)
        cv2.putText(left, "zoom here", (x0, max(14, y0 - 6)),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 220, 255), 1, cv2.LINE_AA)
        right = frame[y0:y0 + ch, x0:x0 + cw]
        right = cv2.resize(right, (iw, ih), interpolation=cv2.INTER_CUBIC)
        h = hoop_of(t)
        if h is not None:
            cx = int((h["cx"] - x0) * scale); cy = int((h["cy"] - y0) * scale)
            rx = int(h["rx"] * scale)
            cv2.ellipse(right, (cx, cy), (rx, max(3, int(rx * 0.3))), 0, 0, 360,
                        (60, 240, 60), 1)
        canvas = np.full((canvas_h, canvas_w, 3), 30, np.uint8)
        canvas[pad:pad + H, pad:pad + W] = left
        canvas[pad:pad + ih, pad + W + gap:pad + W + gap + iw] = right
        cv2.putText(canvas, "rim x4", (pad + W + gap, pad + ih - 6),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.5, (60, 240, 60), 1, cv2.LINE_AA)
        cv2.imwrite(str(out_dir / f"f{f:05d}.png"), canvas,
                    [int(cv2.IMWRITE_PNG_COMPRESSION), 3])
        paths.append(out_dir / f"f{f:05d}.png")
        labels.append(f"{t:6.2f}s")
    cap.release()
    return paths, labels


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--video", required=True)
    ap.add_argument("--hoop-json", default="", help="带 hoop.samples 的 raw_track.json")
    ap.add_argument("--shot", action="append", required=True, help="id:start:end")
    ap.add_argument("--key", action="append", default=[], help="id:start:end（关键段）")
    ap.add_argument("--out", required=True)
    ap.add_argument("--slow", type=int, default=4)
    ap.add_argument("--tight-slow", type=int, default=6)
    ap.add_argument("--zoom-w", type=int, default=340)
    ap.add_argument("--zoom-h", type=int, default=300)
    ap.add_argument("--tight-w", type=int, default=160)
    ap.add_argument("--tight-h", type=int, default=140)
    args = ap.parse_args(argv)

    cv2, _ = _cv2_np()
    video = Path(args.video).resolve()
    if not video.exists():
        print(f"[error] 找不到视频：{video}")
        return 2
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    _hmeta, samples = ({}, [])
    if args.hoop_json:
        _hmeta, samples = load_hoop_samples(Path(args.hoop_json))
    hoop_of = lambda t: hoop_at(samples, t)                            # noqa: E731

    keymap = {}
    for spec in args.key:
        sid, a, b = spec.split(":")
        keymap[sid] = (float(a), float(b))

    cap = cv2.VideoCapture(str(video))
    fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
    total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
    W = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    H = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    print(f"视频 {video.name}  {W}x{H}  {fps:.4f} fps  {total} 帧  时长 {total / fps:.1f}s")

    rows, readme = [], []
    for spec in args.shot:
        sid, s0, s1 = spec.split(":")
        t0, t1 = float(s0), float(s1)
        f0, f1 = int(round(t0 * fps)), int(round(t1 * fps))
        k0, k1 = keymap.get(sid, ((t0 + t1) / 2 - 0.35, (t0 + t1) / 2 + 0.35))
        kf0, kf1 = max(f0, int(round(k0 * fps))), min(f1, int(round(k1 * fps)))
        mid = hoop_of((k0 + k1) / 2)
        print(f"\n=== 候选 {sid}  窗口 {t0:.2f}~{t1:.2f}s  关键 {k0:.2f}~{k1:.2f}s"
              + (f"  筐心≈({mid['cx']:.0f},{mid['cy']:.0f}) rx={mid['rx']:.0f}" if mid else ""))

        # ---- 全画面 ----
        fdir = out / f"shot{sid}_frames"; fdir.mkdir(exist_ok=True)
        paths, labels = [], []
        cap.set(cv2.CAP_PROP_POS_FRAMES, f0)
        for f in range(f0, f1 + 1):
            ok, frame = cap.read()
            if not ok:
                break
            t = f / fps
            cv2.imwrite(str(fdir / f"f{f:05d}.png"), annotate(frame, t, fps),
                        [int(cv2.IMWRITE_PNG_COMPRESSION), 3])
            paths.append(fdir / f"f{f:05d}.png"); labels.append(f"{t:6.2f}s")
        ok, err = write_video(fdir, out / f"shot{sid}_rim_slow{args.slow}x.mp4",
                              fps, args.slow, f0)
        print(f"  ① 全画面慢放 {args.slow}x：{'OK' if ok else '失败 ' + err}")

        # ---- 筐口放大 ----
        if mid:
            zx0 = int(max(0, min(W - args.zoom_w, mid["cx"] - args.zoom_w // 2)))
            zy0 = int(max(0, min(H - args.zoom_h, mid["cy"] - args.zoom_h // 2)))
            zdir = out / f"shot{sid}_frames_zoom"; zdir.mkdir(exist_ok=True)
            zp, zl = crop_frames(cv2, video, f0, f1, fps,
                                 (zx0, zy0, args.zoom_w, args.zoom_h), 2, zdir, hoop_of)
            ok, err = write_video(zdir, out / f"shot{sid}_rim_zoom_slow{args.slow}x.mp4",
                                  fps, args.slow, f0)
            print(f"  ② 筐口放大 2x 慢放 {args.slow}x：{'OK' if ok else '失败 ' + err}")

            # ---- 紧裁剪（关键段，主看这个）----
            tx0 = int(max(0, min(W - args.tight_w, mid["cx"] - args.tight_w // 2)))
            ty0 = int(max(0, min(H - args.tight_h, mid["cy"] - args.tight_h // 2)))
            tdir = out / f"shot{sid}_frames_tight"; tdir.mkdir(exist_ok=True)
            tp, tl = crop_frames(cv2, video, kf0, kf1, fps,
                                 (tx0, ty0, args.tight_w, args.tight_h), 4, tdir, hoop_of)
            ok, err = write_video(tdir, out / f"shot{sid}_rim_tight_slow{args.tight_slow}x.mp4",
                                  fps, args.tight_slow, kf0)
            print(f"  ③ 紧裁剪 4x 慢放 {args.tight_slow}x：{'OK' if ok else '失败 ' + err}")
            for nm, a, b in write_sheets(tp, tl, out / f"shot{sid}_key_zoom_sheet",
                                         f"shot{sid} KEY zoom4x (one tile = one frame)",
                                         4, (640, 560), 2):
                readme.append(f"  - `{nm}`  {a}~{b}（紧凑裁剪，一帧一格）")

            # ---- 并排：左全画面 / 右筐口放大（推荐先看这个）----
            pdir = out / f"shot{sid}_frames_pip"; pdir.mkdir(exist_ok=True)
            pp, pl = make_pip_frames(cv2, video, f0, f1, fps,
                                     (tx0, ty0, args.tight_w, args.tight_h), 4,
                                     pdir, hoop_of)
            ok, err = write_video(pdir, out / f"shot{sid}_full_pip_slow{args.slow}x.mp4",
                                  fps, args.slow, f0)
            print(f"  ④ 左全画面/右筐口放大 并排 慢放 {args.slow}x："
                  f"{'OK' if ok else '失败 ' + err}")

        # ---- 关键段逐帧（全画面缩放版）----
        kp = [p for p, l in zip(paths, labels)
              if k0 <= float(l.replace("s", "")) <= k1]
        kl = [l for l in labels if k0 <= float(l.replace("s", "")) <= k1]
        if kp:
            for nm, a, b in write_sheets(kp, kl, out / f"shot{sid}_key_sheet",
                                         f"shot{sid} KEY full frame",
                                         8, (640, 360), 4):
                readme.append(f"  - `{nm}`  {a}~{b}（全画面）")
        # ---- 整段概览 ----
        for nm, a, b in write_sheets(paths, labels, out / f"shot{sid}_sheet",
                                     f"shot{sid} overview",
                                     24, (426, 240), 6):
            readme.append(f"  - `{nm}`  {a}~{b}（概览）")

        rows.append({"candidate_id": sid, "window_s": f"{t0:.2f}-{t1:.2f}",
                     "frames": f"{f0}-{f1}", "key_s": f"{k0:.2f}-{k1:.2f}",
                     "key_frames": f"{kf0}-{kf1}",
                     "model_hoop_cx": round(mid["cx"], 1) if mid else "",
                     "model_hoop_cy": round(mid["cy"], 1) if mid else "",
                     "model_hoop_rx": round(mid["rx"], 1) if mid else "",
                     "result": "", "confirmed_by": "", "confirmed_at": "", "note": ""})

    cap.release()
    for d in list(out.glob("shot*_frames")) + list(out.glob("shot*_frames_zoom")) \
            + list(out.glob("shot*_frames_tight")) + list(out.glob("shot*_frames_pip")):
        shutil.rmtree(d, ignore_errors=True)

    with (out / "confirm_table.csv").open("w", encoding="utf-8-sig", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=list(rows[0].keys()))
        w.writeheader()
        for r in rows:
            w.writerow(r)

    (out / "素材清单.txt").write_text("\n".join(readme) + "\n", encoding="utf-8")
    print(f"\n待填表：{out / 'confirm_table.csv'}")
    print(f"逐帧表清单：{out / '素材清单.txt'}（{len(readme)} 张）")
    print("result 列填 make / miss / unseen（看不清就写 unseen，不要猜）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
