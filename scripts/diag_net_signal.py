"""换信号：不看球，看**网有没有被带动**。

为什么改这个：
  球在这段素材里只有 7~15px 且运动模糊，四条路（YOLO/运动块/CNN/橙色块）全废，
  用户点球心也点不准（11 个框全落在篮圈上）。继续追球是死路。
  但**球穿过网时，网会被带动而明显晃动** —— 这是发生在固定位置的强信号，
  不需要分辨球本身。

检验方式：用用户标的 14 正 / 18 负当准绳，比较两组的网动特征：
  * net_energy   ：篮圈下方「网所在漏斗区」的运动能量峰值
  * net_ratio    ：net_energy / 圈外环带能量（擦筐而过时环带也动，比值会小）
如果这两组能分开，就有了一条**不依赖球检测**的自动判据。
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


def net_masks(np_, cx, cy, rx, ry, shape):
    H, W = shape
    yy, xx = np_.mgrid[0:H, 0:W]
    dx = (xx - cx) / max(1.0, rx)
    dy = (yy - cy) / max(1.0, ry)
    r = np_.sqrt(dx ** 2 + dy ** 2)
    net = (r <= 1.05) & (yy >= cy - ry * 0.3) & (yy <= cy + ry * 3.2)
    ring = (r > 1.25) & (r <= 2.1) & (yy <= cy + ry * 3.2)
    return net, ring


def main() -> int:
    rows = [json.loads(l) for l in
            FB.read_text(encoding="utf-8").splitlines() if l.strip()]
    rows = [r for r in rows if "bili" in str(r.get("video", ""))]
    hoop = MARKS["hoop"]
    cx, cy, rx, ry = [float(v) for v in hoop]

    net_m, ring_m = net_masks(np, cx, cy, rx, ry, (1080, 1920))
    print(f"篮筐 ({cx:.0f},{cy:.0f}) r=({rx:.0f},{ry:.0f})  "
          f"网区 {int(net_m.sum())}px  环带 {int(ring_m.sum())}px")

    cap = cv2.VideoCapture(str(VID))
    fps = cap.get(cv2.CAP_PROP_FPS) or 24.0
    out = []
    for r in rows:
        t = float(r["t"])
        f0, f1 = int((t - 1.0) * fps), int((t + 0.8) * fps)
        cap.set(cv2.CAP_PROP_POS_FRAMES, max(0, f0))
        prev = None
        best_net = 0.0
        best_ratio = 0.0
        series = []
        for f in range(f0, f1 + 1):
            ok, fr = cap.read()
            if not ok:
                break
            g = cv2.cvtColor(fr, cv2.COLOR_BGR2GRAY)
            if prev is not None:
                d = cv2.absdiff(g, prev)
                db = cv2.GaussianBlur(d, (5, 5), 0)
                en = float(db[net_m].mean())
                er = float(db[ring_m].mean())
                series.append((f / fps, en, er))
                best_net = max(best_net, en)
                if er > 0.02:
                    best_ratio = max(best_ratio, en / er)
            prev = g
        # 峰值处的比值（在 net 最大的那一帧取）
        ratio_at_peak = 0.0
        if series:
            tp = max(series, key=lambda s: s[1])
            ratio_at_peak = tp[1] / max(0.02, tp[2])
        out.append({"t": t, "label": r["label"], "net_peak": round(best_net, 2),
                    "ratio_peak": round(best_ratio, 2),
                    "ratio_at_net_peak": round(ratio_at_peak, 2)})
    cap.release()

    def stat(vals):
        if not vals:
            return "—"
        a = np.array(vals)
        return (f"均值 {a.mean():6.2f}  中位 {np.median(a):6.2f}  "
                f"范围 {a.min():6.2f}~{a.max():6.2f}")

    made = [o for o in out if o["label"] == "made"]
    miss = [o for o in out if o["label"] == "miss"]
    print(f"\n进球 {len(made)} 个 / 没进 {len(miss)} 个")
    for key in ("net_peak", "ratio_peak", "ratio_at_net_peak"):
        print(f"\n{key}:")
        print(f"  进球  {stat([o[key] for o in made])}")
        print(f"  没进  {stat([o[key] for o in miss])}")

    # 只看 net_peak 能否分开
    mv = np.array([o["net_peak"] for o in made])
    nv = np.array([o["net_peak"] for o in miss])
    print(f"\n用 net_peak 分开的效果（阈值扫描，只看精确率/召回）：")
    best = (0.0, 0.0, 0.0)
    for th in np.arange(0.5, max(mv.max(), nv.max()) + 0.5, 0.5):
        tp = int((mv >= th).sum()); fp = int((nv >= th).sum())
        fn = len(mv) - tp
        p = tp / (tp + fp) if tp + fp else 0.0
        rc = tp / (tp + fn) if tp + fn else 0.0
        f1 = 2 * p * rc / (p + rc) if p + rc else 0.0
        if f1 > best[0]:
            best = (f1, float(th), p, rc)
    print(f"  最优 F1 {best[0]:.2f}（阈值 {best[1]:.1f}，精确率 {best[2]:.0%}，"
          f"召回 {best[3]:.0%}）")

    (ROOT / "out" / "net_signal_eval.json").write_text(
        json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    print("\n逐条明细已存 out/net_signal_eval.json")
    print("\n进球（按 net_peak 降序）：")
    for o in sorted(made, key=lambda x: -x["net_peak"]):
        print(f"  t={o['t']:8.2f}  net {o['net_peak']:6.2f}  "
              f"ratio {o['ratio_peak']:5.2f}  ratio@peak {o['ratio_at_net_peak']:5.2f}")
    print("没进（按 net_peak 降序）：")
    for o in sorted(miss, key=lambda x: -x["net_peak"]):
        print(f"  t={o['t']:8.2f}  net {o['net_peak']:6.2f}  "
              f"ratio {o['ratio_peak']:5.2f}  ratio@peak {o['ratio_at_net_peak']:5.2f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
