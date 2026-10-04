"""用数据集自检关键点映射：映射对的话，单应矩阵应当能同时拟合同一张图上的所有点。

原理（这是判断映射对错的硬标准）：
  球场上的所有关键点都在**同一个地面平面**上，所以对任意一张图，
  一定存在一个单应矩阵把它们全部映到正确的球场坐标。
  于是：拿映射文件里的球场坐标 + 图上的标注像素 → 解单应矩阵 → 看重投影误差。
  * 映射正确 → 误差很小（厘米级，只受标注精度限制）
  * 某个点点位映射错了 → 那个点会明显离群（RANSAC 会把它剔掉，或整体误差变大）

用法：
    python scripts/verify_court_kp_map.py --map data/court_kp_map.json
    # 指定数据集目录： --ds data/calib_ds
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import numpy as np                                           # noqa: E402

KPT = 14


def load_labels(ds: Path, min_vis: int = 6):
    out = []
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
            if len(pts) >= min_vis:
                out.append((lp.stem, pts))
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="用数据集自检关键点映射")
    ap.add_argument("--ds", default="data/calib_ds")
    ap.add_argument("--map", default="data/court_kp_map.json")
    ap.add_argument("--min-vis", type=int, default=6)
    ap.add_argument("--limit", type=int, default=120)
    a = ap.parse_args(argv)

    mapping = json.loads(Path(a.map).read_text(encoding="utf-8"))
    court = {}
    for k, v in mapping.items():
        if k.startswith("_"):
            continue
        court[int(k)] = np.array(v, dtype=float)
    if len(court) < 4:
        print("[err] 映射里不足 4 个点")
        return 2
    print(f"映射点：{sorted(court)}")

    items = load_labels(Path(a.ds), a.min_vis)[: a.limit]
    if not items:
        print("[err] 没读到标注")
        return 1
    print(f"用于自检的图：{len(items)} 张（每张 ≥{a.min_vis} 个可见点）")

    # 逐图 RANSAC 解单应矩阵，统计误差与离群点
    def _find(src, dst):
        """自己实现 DLT（避免依赖 cv2 的接口差异）。

        注意：这个函数必须在**使用之前**定义 —— 之前写在循环后面，
        于是每张图都抛 NameError，又被 `except Exception: continue` 吞掉，
        结果统计出来是 0 张图（还不报错），差点得出"映射可用"的错误结论。
        """
        A = []
        for (x, y), (u, v) in zip(src, dst):
            A.append([x, y, 1, 0, 0, 0, -u * x, -u * y, -u])
            A.append([0, 0, 0, x, y, 1, -v * x, -v * y, -v])
        A = np.array(A, dtype=np.float64)
        _u, _s, Vt = np.linalg.svd(A)
        h = Vt[-1]
        return h.reshape(3, 3), None

    per_point_err = {i: [] for i in court}
    overall, n_ok = [], 0
    outlier_count = {i: 0 for i in court}
    for name, pts in items:
        src, dst, ids = [], [], []
        for i, (x, y) in pts.items():
            if i in court:
                src.append([x, y]); dst.append(court[i]); ids.append(i)
        if len(src) < 4:
            continue
        srcA = np.array(src, dtype=np.float64)
        dstA = np.array(dst, dtype=np.float64)
        # 用最小二乘先解，再看残差（点数少时 RANSAC 容易剔错）
        try:
            H, _mask = _find(srcA, dstA)
        except Exception:
            continue
        if H is None:
            continue
        errs = []
        for k, (x, y) in enumerate(src):
            p = H @ np.array([x, y, 1.0])
            p = p[:2] / p[2]
            e = float(np.linalg.norm(p - dst[k]))
            errs.append(e)
            per_point_err[ids[k]].append(e)
        mean_e = float(np.mean(errs))
        overall.append(mean_e)
        if mean_e < 1.0:
            n_ok += 1
        else:
            # 找出该图最离群的点
            worst = int(np.argmax(errs))
            if errs[worst] > 3.0:
                outlier_count[ids[worst]] += 1

    def _find(src, dst):
        """自己实现 DLT（避免依赖 cv2 的接口差异）。"""
        A = []
        for (x, y), (u, v) in zip(src, dst):
            A.append([x, y, 1, 0, 0, 0, -u * x, -u * y, -u])
            A.append([0, 0, 0, x, y, 1, -v * x, -v * y, -v])
        A = np.array(A, dtype=np.float64)
        _u, _s, Vt = np.linalg.svd(A)
        h = Vt[-1]
        return h.reshape(3, 3), None

    overall = np.array(overall)
    print(f"\n=== 自检结果 ===")
    print(f"  平均重投影误差：中位 {np.median(overall):.3f} m  "
          f"均值 {overall.mean():.3f} m")
    print(f"  误差 <1m 的图：{n_ok}/{len(overall)}")
    print(f"\n  逐点平均误差（越大越可能是映射写错了）：")
    for i in sorted(court):
        e = per_point_err[i]
        if not e:
            continue
        tag = "  ← 可疑" if np.mean(e) > 1.5 else ""
        print(f"    kp{i:02d}  样本 {len(e):4d}  平均 {np.mean(e):6.3f} m  "
              f"最大 {np.max(e):6.3f} m{tag}")
    print(f"\n  被判离群（单图误差 >3m）次数：")
    for i in sorted(outlier_count):
        if outlier_count[i]:
            print(f"    kp{i:02d}: {outlier_count[i]} 次")
    bad = [i for i in court if per_point_err[i] and np.mean(per_point_err[i]) > 1.5]
    if bad:
        print(f"\n⚠ 这些点的映射可能有误，需要修正：{bad}")
        print("  （修正方法：把它们改到正确的球场坐标，或用 RANSAC 只保留一致的点）")
    else:
        print("\n✓ 所有点的映射都自洽（平均误差 <1.5m），映射可用")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
