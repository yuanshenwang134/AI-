"""实证推导关键点 → 球场坐标（不靠猜，靠数据集自身的一致性）。

上一版为什么错：我按矢量示意图的比例推坐标，而那张图**比例失真**（画得像方形，
真实球场是 28×15）。自检显示全部点误差 1.6~5.3m → 映射错。

本方法（严谨）：
  1. 只假设**最确定的 4 个点**是场地四角：kp0/kp2/kp5/kp7 = (±7.5, ±14)；
  2. 对每张"四角都可见"的图，用这 4 组对应解出该图的单应矩阵；
  3. 把**其余 10 个关键点**按该单应矩阵**反算成球场坐标**；
  4. 跨所有图取**中位数**作为该点的球场坐标，并看离散度（IQR）。
     * 离散度小 → 说明"四角假设"成立、且该点的坐标被数据一致地确定 ✓
     * 离散度大 → 该点标注不一致（或四角假设不成立）
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
import numpy as np                                            # noqa: E402

KPT = 14
CORNERS = {0: (-7.5, 14.0), 2: (7.5, 14.0), 5: (7.5, -14.0), 7: (-7.5, -14.0)}


def dlt(src, dst):
    A = []
    for (x, y), (u, v) in zip(src, dst):
        A.append([x, y, 1, 0, 0, 0, -u * x, -u * y, -u])
        A.append([0, 0, 0, x, y, 1, -v * x, -v * y, -v])
    A = np.array(A, dtype=np.float64)
    _u, _s, Vt = np.linalg.svd(A)
    return Vt[-1].reshape(3, 3)


def main() -> int:
    ds = ROOT / "data" / "calib_ds"
    per_point = {i: [] for i in range(KPT)}
    used = 0
    for split in ("train", "valid", "test"):
        for lp in (ds / split / "labels").glob("*.txt"):
            txt = lp.read_text(encoding="utf-8").strip()
            if not txt:
                continue
            parts = txt.split()
            if len(parts) < 5 + KPT * 3:
                continue
            v = [float(x) for x in parts[5:5 + KPT * 3]]
            pts = {i: (v[i * 3], v[i * 3 + 1]) for i in range(KPT)
                   if int(v[i * 3 + 2]) > 0}
            if not all(k in pts for k in CORNERS):
                continue
            src = [pts[k] for k in CORNERS]
            dst = [CORNERS[k] for k in CORNERS]
            H = dlt(src, dst)
            used += 1
            for i, (x, y) in pts.items():
                if i in CORNERS:
                    continue
                p = H @ np.array([x, y, 1.0])
                if abs(p[2]) < 1e-9:
                    continue
                per_point[i].append((p[0] / p[2], p[1] / p[2]))
    print(f"用上 {used} 张「四角都可见」的图")
    if used < 5:
        print("[err] 样本太少，结论不可靠")
        return 1

    print(f"\n{'点':>4} {'样本':>5} {'球场坐标(中位)':>22} {'离散度(IQR)':>14}  判断")
    out = {}
    ok_all = True
    for i in range(KPT):
        if i in CORNERS:
            out[str(i)] = list(CORNERS[i])
            print(f"{i:>4} {'—':>5} {str(list(CORNERS[i])):>22} {'（假设值）':>14}  四角")
            continue
        arr = np.array(per_point[i])
        if len(arr) < 5:
            print(f"{i:>4} {len(arr):>5} {'样本不足':>22}")
            continue
        med = np.median(arr, axis=0)
        iqr = np.percentile(arr, 75, axis=0) - np.percentile(arr, 25, axis=0)
        spread = float(np.max(iqr))
        good = spread < 1.0
        ok_all = ok_all and good
        out[str(i)] = [round(float(med[0]), 2), round(float(med[1]), 2)]
        print(f"{i:>4} {len(arr):>5} {str([round(float(med[0]),2), round(float(med[1]),2)]):>22} "
              f"{spread:>14.2f}  {'✓ 稳定' if good else '⚠ 离散大'}")

    mp = ROOT / "data" / "court_kp_map.json"
    mp.write_text(json.dumps({
        "_说明": "由 scripts/derive_court_kp_map.py 从数据集**实证推导**："
                 "以四角(0/2/5/7)为锚，逐图解单应矩阵后反算其余点并取中位。",
        "_方法": "FIBA 28x15 米，原点中圈中心，x=宽度±7.5，y=长度±14",
        **out}, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n已写出 {mp}")
    print("✓ 全部点离散度 <1m，映射可用" if ok_all
          else "⚠ 有点离散度大 → 该点标注可能不一致，使用时建议只取离散小的点")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
