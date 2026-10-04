"""诊断：候选的「球块链」到底有没有跟住球？

两条路都失败了（CNN 样本外≈瞎猜；几何判据 F1 0.29）。在继续之前必须确认
**输入是否可信**：扫描器挑出来的"球块"是不是球。
如果链本身就是垃圾（挑到了球员的头/手/网），那任何判据都不可能work。

做法：把用户明确判为「进球」和「没进」的各挑几个，打印它们的链
（时刻、球心像素、相对篮圈位置、块面积），并生成一张叠加图看球块落在哪。
"""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

import cv2
import numpy as np

CANDS = json.loads((ROOT / "out/label_bili/candidates.json").read_text(encoding="utf-8"))
cands = CANDS["candidates"]
fb = [json.loads(x) for x in
      (ROOT / "data/basket_feedback.jsonl").read_text(encoding="utf-8").splitlines()
      if x.strip()]
fb = [r for r in fb if "bili" in str(r.get("video", ""))]

print("篮筐", CANDS["meta"]["hoop"], "窗口", CANDS["meta"]["window"])
made_ts = sorted(float(r["t"]) for r in fb if r["label"] == "made")
miss_ts = sorted(float(r["t"]) for r in fb if r["label"] == "miss")

cap = cv2.VideoCapture(str(ROOT / "data" / "bili_nybo.mp4"))
fps = cap.get(cv2.CAP_PROP_FPS)

for tag, ts in (("进球", made_ts[:3]), ("没进", miss_ts[:3])):
    for t in ts:
        c = None
        for x in cands:
            if abs(float(x["t0"]) - t) < 1.0:
                c = x
                break
        if c is None:
            print(f"\n[{tag}] t={t} 找不到候选")
            continue
        cx, cy, rx, ry = c["hoop"]
        print(f"\n[{tag}] t={t:.2f}s  n={len(c['chain'])}  "
              f"下落={c['drop_px']:.0f}px  球心最近居圈心={c['rel_x_at_rim']:+.2f}rx")
        print("   时刻     球心(px)     居圈心x   居圈心y    面积")
        for (tt, bx, by, a) in c["chain"][:14]:
            print(f"   {tt:8.3f}  ({bx:6.1f},{by:6.1f})  "
                  f"{(bx - cx) / rx:+7.2f}  {(by - cy) / ry:+7.2f}   {a:5d}")
        # 叠加图：第一帧 + 该帧的球块位置
        t0 = c["chain"][0][0]
        cap.set(cv2.CAP_PROP_POS_FRAMES, int(t0 * fps))
        ok, fr = cap.read()
        if ok:
            x0, y0, x1, y1 = [int(v) for v in c["win"]]
            pad = 40
            X0, Y0 = max(0, x0 - pad), max(0, y0 - pad)
            X1, Y1 = x1 + pad, y1 + pad
            crop = fr[Y0:Y1, X0:X1].copy()
            cv2.ellipse(crop, (int(cx - X0), int(cy - Y0)),
                        (max(2, int(rx)), max(2, int(ry))), 0, 0, 360,
                        (0, 0, 255), 2)
            for (tt, bx, by, a) in c["chain"]:
                if X0 <= bx <= X1 and Y0 <= by <= Y1:
                    cv2.circle(crop, (int(bx - X0), int(by - Y0)), 8,
                               (0, 255, 255), 2)
            cv2.putText(crop, f"{tag} t={t:.1f} drop={c['drop_px']:.0f}",
                        (6, 20), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (255, 255, 255), 2)
            out = ROOT / "out" / f"chain_{tag}_{int(t)}.png"
            cv2.imwrite(str(out), cv2.resize(crop, None, fx=3.0, fy=3.0,
                                             interpolation=cv2.INTER_LANCZOS4))
            print(f"   → {out.name}")
cap.release()
