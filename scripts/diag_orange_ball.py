"""最后一试：用「橙色 + 圆形 + 尺寸」直接在筐口找球，看能不能重建真轨迹。

为什么还值得试：检测器失效、运动块是噪声，但球本身有稳定外观特征
（橙棕色、近似圆形、尺寸随距离基本固定）。如果颜色+形状能稳定找到球，
就能重建真轨迹，几何判据就有意义。

做法：在用户确认的进球时刻，逐帧在筐口邻域里找"橙色圆形块"，
打印位置序列 —— 如果位置连续、且朝下穿过筐口，就说明这条路可行。
同时把球的位置画在图上，便于我确认找对了。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
import cv2
import numpy as np

VID = ROOT / "data" / "bili_nybo.mp4"
fb = [json.loads(x) for x in
      (ROOT / "data/basket_feedback.jsonl").read_text(encoding="utf-8").splitlines()
      if x.strip()]
fb = [r for r in fb if "bili" in str(r.get("video", ""))]
made = sorted(float(r["t"]) for r in fb if r["label"] == "made")

HOOP = (60.0, 30.0, 45.0, 13.0)
cx, cy, rx, ry = HOOP
WIN = (6, 0, 114, 109)


def find_ball(cv2, np_, frame, win):
    """在窗口里找橙色圆形块，返回 (x, y, 半径, 橙色占比) 或 None。"""
    x0, y0, x1, y1 = win
    roi = frame[y0:y1, x0:x1]
    if roi.size == 0:
        return None
    hsv = cv2.cvtColor(roi, cv2.COLOR_BGR2HSV)
    # 橙色范围（篮球的橙棕）：H 5~25，S 较高，V 中等以上
    m = cv2.inRange(hsv, (5, 70, 60), (30, 255, 255))
    m = cv2.morphologyEx(m, cv2.MORPH_OPEN, np.ones((3, 3), np.uint8))
    m = cv2.morphologyEx(m, cv2.MORPH_CLOSE, np.ones((3, 3), np.uint8))
    n, lab, stats, cent = cv2.connectedComponentsWithStats(m, 8)
    best = None
    for i in range(1, n):
        a = int(stats[i, 4])
        w, h = int(stats[i, 2]), int(stats[i, 3])
        if a < 25 or a > 2000:
            continue
        ar = w / max(1.0, h)
        if not (0.55 <= ar <= 1.8):
            continue
        fill = a / max(1.0, w * h)
        if fill < 0.5:
            continue
        # 圆形度：用外接框近似，越接近 1 越像球
        score = fill * (1.0 - abs(ar - 1.0))
        if best is None or score > best[0]:
            best = (score, float(cent[i][0]) + x0, float(cent[i][1]) + y0,
                    (w + h) / 4.0, a)
    return best


cap = cv2.VideoCapture(str(VID))
fps = cap.get(cv2.CAP_PROP_FPS)

for t in made[:3]:
    print(f"\n[进球] t={t:.2f}s：橙色圆块检测")
    f0 = int((t - 0.7) * fps)
    seq = []
    tiles = []
    for k in range(int(1.5 * fps)):
        cap.set(cv2.CAP_PROP_POS_FRAMES, f0 + k)
        ok, fr = cap.read()
        if not ok:
            break
        tt = (f0 + k) / fps
        b = find_ball(cv2, np, fr, WIN)
        if b:
            seq.append((tt, b[1], b[2], b[3], b[4]))
        # 存几帧做可视化
        if k % 3 == 0 and len(tiles) < 12:
            x0, y0, x1, y1 = WIN
            crop = fr[y0:y1, x0:x1].copy()
            cv2.ellipse(crop, (int(cx - x0), int(cy - y0)),
                        (int(rx), int(ry)), 0, 0, 360, (0, 0, 255), 2)
            if b:
                cv2.circle(crop, (int(b[1] - x0), int(b[2] - y0)),
                           max(6, int(b[3])), (0, 255, 0), 2)
            cv2.putText(crop, f"{tt:.2f}", (4, 16),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.45, (255, 255, 255), 1)
            tiles.append(cv2.resize(crop, None, fx=2.2, fy=2.2,
                                    interpolation=cv2.INTER_LANCZOS4))
    if seq:
        print(f"   检出 {len(seq)} 帧，位置序列：")
        for tt, bx, by, r, a in seq[:16]:
            print(f"      t={tt:8.2f}  ({bx:6.1f},{by:6.1f})  半径~{r:4.1f}px  "
                  f"面积 {a:4d}  居圈心x {(bx - cx) / rx:+.2f}  "
                  f"居圈心y {(by - cy) / ry:+.2f}")
    else:
        print("   ✗ 一帧都没找到橙色圆块")
    if tiles:
        rows = []
        for i in range(0, len(tiles), 4):
            chunk = tiles[i:i + 4]
            hh = max(x.shape[0] for x in chunk)
            chunk = [cv2.copyMakeBorder(x, 0, hh - x.shape[0], 0, 0,
                                        cv2.BORDER_CONSTANT, value=(0, 0, 0))
                     for x in chunk]
            rows.append(np.hstack(chunk))
        ww = max(r.shape[1] for r in rows)
        rows = [cv2.copyMakeBorder(r, 0, 0, 0, ww - r.shape[1],
                                   cv2.BORDER_CONSTANT, value=(0, 0, 0))
                for r in rows]
        out = ROOT / "out" / f"orange_{int(t)}.png"
        cv2.imwrite(str(out), np.vstack(rows))
        print(f"   → {out.name}")
cap.release()
