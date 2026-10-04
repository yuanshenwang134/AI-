"""决定性检验：现成的球检测器在这段素材上认不认得出球？

背景：候选的"球块链"是噪声（位置乱跳、面积差 8 倍），所以 CNN 学噪声、
几何判据算噪声。要往下走，前提是**能真的追到球**。

这个脚本在用户明确判为「进球」的时刻附近，用仓库里的球检测器
（runs/detect/ball/weights/best.pt）逐帧检测，看：
  * 有没有检出球类？
  * 球的位置是否连续、是否朝篮筐去？
如果检测器在这段素材上有效，就能用它替代 _pick_blob 重建轨迹；
如果无效，就得承认"这段素材上自动判定不成立"，必须换方案（重训检测器/人工）。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import cv2
import numpy as np

WEIGHTS = ROOT / "runs/detect/ball/weights/best.pt"
VID = ROOT / "data" / "bili_nybo.mp4"

fb = [json.loads(x) for x in
      (ROOT / "data/basket_feedback.jsonl").read_text(encoding="utf-8").splitlines()
      if x.strip()]
fb = [r for r in fb if "bili" in str(r.get("video", ""))]
made = sorted(float(r["t"]) for r in fb if r["label"] == "made")
miss = sorted(float(r["t"]) for r in fb if r["label"] == "miss")

print(f"权重 {WEIGHTS.name} 存在={WEIGHTS.exists()}")
if not WEIGHTS.exists():
    print("[err] 没有球检测权重")
    raise SystemExit(2)

from ultralytics import YOLO
model = YOLO(str(WEIGHTS))

cap = cv2.VideoCapture(str(VID))
fps = cap.get(cv2.CAP_PROP_FPS)
HOOP = (60.0, 30.0, 45.0, 13.0)

for tag, ts in (("进球", made[:3]), ("没进", miss[:2])):
    for t in ts:
        print(f"\n[{tag}] t={t:.2f}s：逐帧检测（只报篮筐附近 150px 内的）")
        f0 = int((t - 0.8) * fps)
        hits = []
        for k in range(int(1.6 * fps)):
            cap.set(cv2.CAP_PROP_POS_FRAMES, f0 + k)
            ok, fr = cap.read()
            if not ok:
                break
            r = model.predict(fr, verbose=False, device="cpu", conf=0.15)[0]
            tt = (f0 + k) / fps
            best = None
            for b in r.boxes:
                x1, y1, x2, y2 = [float(v) for v in b.xyxy[0]]
                cf = float(b.conf[0])
                bx, by = (x1 + x2) / 2, (y1 + y2) / 2
                d = np.hypot(bx - HOOP[0], by - HOOP[1])
                if best is None or d < best[0]:
                    best = (d, bx, by, cf, x2 - x1)
            if best and best[0] < 150:
                hits.append((tt, best))
        if not hits:
            print("   ✗ 篮筐附近 150px 内**一帧都没检出球**")
        else:
            print(f"   ✓ 检出 {len(hits)} 帧；前 8 帧：")
            for tt, (d, bx, by, cf, w) in hits[:8]:
                print(f"      t={tt:8.2f}  球({bx:6.1f},{by:6.1f})  "
                      f"距筐 {d:5.0f}px  置信 {cf:.2f}  宽 {w:.0f}px")
cap.release()
