"""用「黄+蓝」颜色特征定位球（球是黄蓝配色，视觉上有辨识度）。

为什么再试一次颜色：前面用"橙色"是错的 —— 这段素材的球是**黄蓝配色**
（放大帧里能看到黄蓝拖影穿过篮网），不是橙棕色。所以颜色范围一直没选对。

做法：在篮筐邻域里找黄、蓝两种颜色同时出现的连通块（球的两半），
检查它们在 14 个进球时刻能否给出**连续、朝下穿过圈内**的轨迹。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
import cv2      # noqa: E402
import numpy as np  # noqa: E402

VID = ROOT / "data" / "bili_nybo.mp4"
MARKS = json.loads((ROOT / "data/marks_bili_nybo.json").read_text(encoding="utf-8"))
FB = ROOT / "data" / "basket_feedback.jsonl"
REGION = (0, 0, 200, 120)      # 篮筐邻域（原图像素）


def yellow_blue_mask(cv2, np_, rgb):
    """黄 + 蓝的掩膜（球的两个色块）。"""
    hsv = cv2.cvtColor(rgb, cv2.COLOR_BGR2HSV)
    y = cv2.inRange(hsv, (18, 90, 90), (40, 255, 255))     # 黄绿
    b = cv2.inRange(hsv, (85, 60, 60), (135, 255, 255))    # 蓝
    m = cv2.bitwise_or(y, b)
    return cv2.morphologyEx(m, cv2.MORPH_OPEN, np_.ones((2, 2), np_.uint8))


def locate(cv2, np_, rgb, x0, y0):
    """在区域里找「黄蓝混在一起」的块（球）；返回 (x,y,area,score) 或 None。"""
    roi = rgb[y0:y0 + REGION[3], x0:x0 + REGION[2]]
    if roi.size == 0:
        return None
    m = yellow_blue_mask(cv2, np_, roi)
    n, lab, stats, cent = cv2.connectedComponentsWithStats(m, 8)
    best = None
    for i in range(1, n):
        a = int(stats[i, 4])
        w, h = int(stats[i, 2]), int(stats[i, 3])
        if a < 20 or a > 1500:
            continue
        sub = (lab == i)
        # 这一块里黄、蓝各自占比（球的两半都有）
        hsv = cv2.cvtColor(roi, cv2.COLOR_BGR2HSV)
        hh = hsv[:, :, 0][sub]
        yy = float(((hh >= 18) & (hh <= 40)).mean())
        bb = float(((hh >= 85) & (hh <= 135)).mean())
        if yy < 0.08 or bb < 0.08:
            continue
        ar = w / max(1.0, h)
        if ar > 3.2:                      # 拖影会比较长，放宽但别太夸张
            continue
        score = a * (1.0 - abs(ar - 1.0) * 0.3) * min(yy, bb) * 2
        if best is None or score > best[0]:
            best = (score, float(cent[i][0]) + x0, float(cent[i][1]) + y0,
                    a, (w, h), round(yy, 2), round(bb, 2))
    return best


def main() -> int:
    rows = [json.loads(l) for l in
            FB.read_text(encoding="utf-8").splitlines() if l.strip()]
    rows = [r for r in rows if "bili" in str(r.get("video", ""))]
    made = sorted(float(r["t"]) for r in rows if r["label"] == "made")
    miss = sorted(float(r["t"]) for r in rows if r["label"] == "miss")
    hoop = MARKS["hoop"]
    cx, cy, rx, ry = [float(v) for v in hoop]
    x0, y0 = REGION[0], REGION[1]
    print(f"篮筐 ({cx:.0f},{cy:.0f})  检测区域 x{x0}-{x0+REGION[2]} y{y0}-{y0+REGION[3]}")

    cap = cv2.VideoCapture(str(VID))
    fps = cap.get(cv2.CAP_PROP_FPS) or 24.0

    def scan(t, span=(-0.7, 0.5)):
        f0, f1 = int((t + span[0]) * fps), int((t + span[1]) * fps)
        seq = []
        cap.set(cv2.CAP_PROP_POS_FRAMES, max(0, f0))
        for f in range(f0, f1 + 1):
            ok, fr = cap.read()
            if not ok:
                break
            b = locate(cv2, np, fr, x0, y0)
            if b:
                seq.append({"t": f / fps, "x": b[1], "y": b[2], "area": b[3],
                            "wh": b[4], "yfrac": b[5], "bfrac": b[6]})
        return seq

    print("\n=== 进球时刻：黄蓝块轨迹 ===")
    n_ok = 0
    for t in made:
        seq = scan(t)
        if len(seq) < 4:
            print(f"  t={t:8.2f}  只找到 {len(seq)} 帧 → 不连续")
            continue
        xs = [p["x"] for p in seq]; ys = [p["y"] for p in seq]
        hop = np.hypot(np.diff(xs), np.diff(ys))
        # 是否朝筐心靠近
        d0 = np.hypot(xs[0] - cx, ys[0] - cy)
        dmin = min(np.hypot(x - cx, y - cy) for x, y in zip(xs, ys))
        ok = (np.median(hop) < 25) and (dmin < 60)
        n_ok += 1 if ok else 0
        print(f"  t={t:8.2f}  帧数 {len(seq):3d}  跳变中位 {np.median(hop):5.1f}px  "
              f"离筐最近 {dmin:5.1f}px  首帧距离 {d0:5.1f}px  "
              f"{'✓ 轨迹合理' if ok else '✗'}")
    print(f"  → {n_ok}/{len(made)} 个进球的轨迹合理")

    print("\n=== 没进时刻（对照）===")
    n_bad = 0
    for t in miss[:6]:
        seq = scan(t)
        if len(seq) < 4:
            print(f"  t={t:8.2f}  只找到 {len(seq)} 帧")
            continue
        xs = [p["x"] for p in seq]; ys = [p["y"] for p in seq]
        hop = np.hypot(np.diff(xs), np.diff(ys))
        dmin = min(np.hypot(x - cx, y - cy) for x, y in zip(xs, ys))
        ok = (np.median(hop) < 25) and (dmin < 60)
        n_bad += 1 if ok else 0
        print(f"  t={t:8.2f}  帧数 {len(seq):3d}  跳变中位 {np.median(hop):5.1f}px  "
              f"离筐最近 {dmin:5.1f}px  {'（也被判合理）' if ok else ''}")
    cap.release()
    print(f"  → 没进里有 {n_bad}/6 个也被判成合理轨迹（假阳性）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
