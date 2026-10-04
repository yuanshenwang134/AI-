"""认数据集的 14 个关键点分别是什么（data.yaml 没给 kpt_names，只能自己看）。

做法：
  1. 统计每张图**可见关键点**的个数（v=2 才算可见）——
     这决定我们能不能从单帧解出可靠的单应矩阵（至少 4 个，最好 8+）；
  2. 挑可见点最多的几张图，把关键点**带编号**画出来 →
     对照球场几何就能认出每个编号对应哪里（底线角/罚球线/篮圈…）。
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
import cv2                                                    # noqa: E402
import numpy as np                                            # noqa: E402

DS = ROOT / "data" / "calib_ds"
KPT = 14


def parse_label(p: Path):
    """解析 YOLO-pose 行 → [(x, y, v)] × 14（像素坐标由调用方换算）。"""
    txt = p.read_text(encoding="utf-8").strip()
    if not txt:
        return None
    parts = txt.split()
    if len(parts) < 5 + KPT * 3:
        return None
    vals = [float(v) for v in parts[5:5 + KPT * 3]]
    return [(vals[i * 3], vals[i * 3 + 1], int(vals[i * 3 + 2]))
            for i in range(KPT)]


def main() -> int:
    rows = []
    for split in ("train", "valid", "test"):
        for lp in (DS / split / "labels").glob("*.txt"):
            kps = parse_label(lp)
            if kps is None:
                continue
            vis = sum(1 for _x, _y, v in kps if v > 0)
            rows.append({"label": lp, "split": split, "vis": vis, "kps": kps})
    if not rows:
        print("[err] 没解析到标注")
        return 1
    vis_counts = np.array([r["vis"] for r in rows])
    print(f"共 {len(rows)} 张标注；每张**可见**关键点数：")
    for q in (0, 25, 50, 75, 90, 100):
        print(f"   {q:>3} 分位: {np.percentile(vis_counts, q):.0f}")
    for n in (4, 6, 8, 10, 12):
        print(f"   可见 ≥{n} 个的图：{int((vis_counts >= n).sum())} 张")
    print(f"   （14 个点里平均可见 {vis_counts.mean():.1f} 个）")

    rows.sort(key=lambda r: -r["vis"])
    # 画出可见点最多的 2 张
    tiles = []
    for r in rows[:2]:
        img_p = DS / r["split"] / "images" / (r["label"].stem + ".jpg")
        if not img_p.exists():
            cands = list((DS / r["split"] / "images").glob(r["label"].stem + ".*"))
            if not cands:
                continue
            img_p = cands[0]
        im = cv2.imread(str(img_p))
        if im is None:
            continue
        H, W = im.shape[:2]
        vis = im.copy()
        for i, (x, y, v) in enumerate(r["kps"]):
            if v <= 0:
                continue
            px, py = int(x * W), int(y * H)
            cv2.circle(vis, (px, py), 6, (0, 255, 0), -1)
            cv2.putText(vis, str(i), (px + 7, py - 7),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.8, (0, 0, 255), 2)
        cv2.putText(vis, f"{img_p.name[:20]} vis={r['vis']}", (8, 26),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.7, (255, 255, 0), 2)
        tiles.append(cv2.resize(vis, (760, int(760 * H / W))))
    if tiles:
        h = min(t.shape[0] for t in tiles)
        out = ROOT / "out" / "calib_kp_layout.png"
        cv2.imwrite(str(out), np.vstack([t[:h] for t in tiles]))
        print(f"\n关键点编号示意图已存 {out}（两张可见点最多的图）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
