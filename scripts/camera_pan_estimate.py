# -*- coding: utf-8 -*-
"""量一段素材的**镜头位移轨迹**（相位相关），用来判断"筐心位移"能不能用摇镜解释。

为什么需要：单看"判罚用筐的 x 跨度大"**不能**证明"两个篮筐互跳"——
摇镜也会让同一个筐在画面上移动。判据是：同一段时间里
`筐心位移 ≈ 镜头位移` → 可以是同一个筐；`筐心位移 ≫ 镜头位移` → 筐身份变了。

方法：每 N 帧取一张、缩到 480×270 灰度，用 `cv2.phaseCorrelate` 求相邻两帧的平移，
按缩放比还原到原分辨率后累加。球员运动会给结果带来几十像素量级的干扰，
所以它只适合做"量级判断"，不能当精确标定。

用法：
    python scripts/camera_pan_estimate.py <视频> [--step 15] [--at 3.37 12.75 17.08]
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("video")
    ap.add_argument("--step", type=int, default=15, help="每隔多少帧测一次（默认 15）")
    ap.add_argument("--width", type=int, default=480, help="缩放后的分析宽度")
    ap.add_argument("--at", type=float, nargs="*", default=[],
                    help="要输出的时刻（秒），会打印该时刻的累计位移")
    ap.add_argument("--out", default="")
    args = ap.parse_args()

    import cv2  # 延迟导入：只有真要量的时候才需要 cv2

    cap = cv2.VideoCapture(str(args.video))
    if not cap.isOpened():
        print("打不开视频：", args.video)
        return 2
    fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
    W = cap.get(cv2.CAP_PROP_FRAME_WIDTH) or 1920.0
    H = cap.get(cv2.CAP_PROP_FRAME_HEIGHT) or 1080.0
    aw = int(args.width)
    ah = max(1, int(round(aw * H / W)))
    sx, sy = W / aw, H / ah

    prev = None
    cx = cy = 0.0
    i = 0
    traj = []
    max_step = 0.0
    while True:
        ok, frame = cap.read()
        if not ok:
            break
        if i % args.step == 0:
            g = cv2.cvtColor(cv2.resize(frame, (aw, ah)), cv2.COLOR_BGR2GRAY).astype("float32")
            if prev is not None:
                (dx, dy), _resp = cv2.phaseCorrelate(prev, g)
                dx *= sx
                dy *= sy
                cx += dx
                cy += dy
                max_step = max(max_step, abs(dx))
            traj.append((round(i / fps, 3), round(cx, 1), round(cy, 1)))
            prev = g
        i += 1
    cap.release()

    print("视频 %s  %dx%d  %.1ffps  共 %d 帧" % (Path(args.video).name, W, H, fps, i))
    print("采样步长 %d 帧（%.2fs），测点 %d 个" % (args.step, args.step / fps, len(traj)))
    if traj:
        print("累计位移：dx=%.0fpx  dy=%.0fpx ；单次采样最大横向位移 %.1fpx"
              % (traj[-1][1], traj[-1][2], max_step))
    for t in args.at:
        if not traj:
            break
        best = min(traj, key=lambda r: abs(r[0] - t))
        print("   t=%-7s 附近（采样点 %.2fs）累计 dx=%.0f dy=%.0f" % (t, best[0], best[1], best[2]))

    if args.out:
        Path(args.out).write_text(
            json.dumps({"video": str(args.video), "step": args.step,
                        "max_step_px": round(max_step, 1), "trajectory": traj},
                       ensure_ascii=False, indent=2), encoding="utf-8")
        print("明细：%s" % args.out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
