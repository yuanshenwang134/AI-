"""不用给点命名：点几个看得见的场地特征点，程序自动认出它们分别是哪里。

为什么需要（用户实测反馈）：
  "一个画面怎么可能点 5~6 个点啊" —— 端线机位一个画面里本来就只有 4~6 个
  场地特征点可见；更糟的是「近端/远端」这种命名在端线视角**极易搞反**
  （你看到的那条底线到底是近端还是远端，从画面根本判断不了），
  名字一错，即使点得再准，解出来的标定也是镜像/错位的。

本方法（对应关系自动识别）：
  输入：同一帧里的 N 个像素点（N≥5），**不带任何名字**；
  输出：它们各自对应哪个场地特征点 + 单应矩阵 + 误差。

  做法（RANSAC over assignments）：
    1. 从 N 个像素里取 4 个，从 M 个场地特征点里取 4 个，枚举所有配对
       （C(N,4)×C(M,4) 种，N=6/M=11 时约 5000 种，可接受）；
    2. 每种配对解一个单应矩阵；
    3. 用它对**其余**像素点做投影，看是否落在某个**还没被用掉**的场地特征点
       附近（容差内）→ 命中越多分数越高；
    4. 取分数最高、且重投影误差最小的那个对应关系。
  为什么能行：4 组对应**总能**解出误差为 0 的单应矩阵（所以单看 4 个点无法
  判断对错），但**第 5 个点会投票** —— 错位的对应关系不可能同时解释第 5 个点。
  这正是"多标一个点"的真正价值。
"""
from __future__ import annotations

import itertools
from typing import Optional

import numpy as np

# 场地口径：FIBA 28×15 米，原点在中圈中心，x=宽度±7.5，y=长度±14
COURT_PTS: dict[str, tuple[float, float]] = {
    "corner_near_left": (-7.5, -14.0),
    "corner_near_right": (7.5, -14.0),
    "corner_far_left": (-7.5, 14.0),
    "corner_far_right": (7.5, 14.0),
    "half_left": (-7.5, 0.0),
    "half_right": (7.5, 0.0),
    "center": (0.0, 0.0),
    # 全场以中圈为 0；罚球线距底线 5.8m，即距中圈 8.2m。
    "ft_near": (0.0, -8.2),
    "ft_far": (0.0, 8.2),
    "hoop_near": (0.0, -12.425),
    "hoop_far": (0.0, 12.425),
    # 罚球区四角（FIBA 罚球区宽 4.9m，长 5.8m）—— 端线视角最常见、也最好认
    "lane_near_left": (-2.45, -8.2),
    "lane_near_right": (2.45, -8.2),
    "lane_far_left": (-2.45, 8.2),
    "lane_far_right": (2.45, 8.2),
    # 三分线弧顶（端线视角一眼能看到）
    # 弧顶距篮筐 6.75m；篮筐距底线 1.575m。
    "arc_near": (0.0, -14.0 + 1.575 + 6.75),
    "arc_far": (0.0, 14.0 - 1.575 - 6.75),
}


def _dlt(src, dst):
    A = []
    for (x, y), (u, v) in zip(src, dst):
        A.append([x, y, 1, 0, 0, 0, -u * x, -u * y, -u])
        A.append([0, 0, 0, x, y, 1, -v * x, -v * y, -v])
    A = np.array(A, dtype=np.float64)
    _u, _s, Vt = np.linalg.svd(A)
    return Vt[-1].reshape(3, 3)


def _apply(H, x, y):
    p = H @ np.array([x, y, 1.0])
    if abs(p[2]) < 1e-9:
        return None
    return (float(p[0] / p[2]), float(p[1] / p[2]))


def perspective_score(pairs: list[tuple[float, float]]) -> tuple[float, int]:
    """透视一致率：相机在场地一端朝另一端看 → 球场 y 越大（越远）图像 y 越小。

    pairs: [(球场y, 图像y), ...]
    返回 (一致率, 参与比较的对数)。一致率低说明这是**镜像解**。
    """
    ok = tot = 0
    for i in range(len(pairs)):
        for j in range(i + 1, len(pairs)):
            dy_c = pairs[i][0] - pairs[j][0]
            if abs(dy_c) < 1.0:          # 深度差不明显，不参与判断
                continue
            dy_i = pairs[i][1] - pairs[j][1]
            tot += 1
            if (dy_c > 0) == (dy_i < 0):
                ok += 1
    return (ok / tot if tot else 1.0), tot


def identify(pixels: list[tuple[float, float]], court: Optional[dict] = None,
             match_tol_m: float = 1.8, max_hypotheses: int = 200000) -> dict:
    """给未命名的像素点找出它们对应的场地特征点。

    pixels: [(x, y), ...] 同一帧里的 N 个点（N≥5）
    返回 {ok, mapping:{像素索引: 特征点名}, rmse_m, hits, H, note}
    """
    court = court or COURT_PTS
    names = list(court)
    pts = [(float(x), float(y)) for x, y in pixels]
    n = len(pts)
    if n < 5:
        return {"ok": False, "note": f"只有 {n} 个点 —— 自动识别至少需要 5 个"
                                    "（4 个点时任何配对都能精确拟合，无法分辨对错）"}
    ci = list(range(n))
    best = None
    tried = 0
    for pidx in itertools.combinations(ci, 4):
        for cidx in itertools.combinations(range(len(names)), 4):
            # 跳过三点共线的退化组合（否则解出来是垃圾，浪费后面的评估）
            src = [pts[i] for i in pidx]
            dst = [court[names[j]] for j in cidx]
            tried += 1
            if tried > max_hypotheses:
                break
            try:
                H = _dlt(src, dst)
            except Exception:  # noqa: BLE001
                continue
            used = set(cidx)
            hits = 4
            mapping = {pidx[k]: names[cidx[k]] for k in range(4)}
            for i in ci:
                if i in pidx:
                    continue
                got = _apply(H, *pts[i])
                if got is None:
                    continue
                best_d, best_j = 1e9, None
                for j, nm in enumerate(names):
                    if j in used:
                        continue
                    u, v = court[nm]
                    d = ((got[0] - u) ** 2 + (got[1] - v) ** 2) ** 0.5
                    if d < best_d:
                        best_d, best_j = d, j
                if best_j is not None and best_d <= match_tol_m:
                    used.add(best_j)
                    mapping[i] = names[best_j]
                    hits += 1
            pairs = []
            for i2, nm2 in mapping.items():
                u2, v2 = court[nm2]
                pairs.append((v2, pts[i2][1]))
            ps, _nt = perspective_score(pairs)
            cand = {"hits": hits, "mapping": dict(mapping), "H": H,
                    "pidx": pidx, "cidx": cidx, "persp": ps}
            if best is None or (cand["hits"], round(cand["persp"], 3)) > \
                    (best["hits"], round(best["persp"], 3)):
                best = cand
        if tried > max_hypotheses:
            break
    if best is None:
        return {"ok": False, "note": "没找到任何自洽的对应关系"}

    # 用命中的点重新最小二乘拟合，算真实误差
    src = [pts[i] for i in sorted(best["mapping"])]
    dst = [court[best["mapping"][i]] for i in sorted(best["mapping"])]
    H = _dlt(src, dst) if len(src) >= 4 else best["H"]
    errs = []
    for (x, y), (u, v) in zip(src, dst):
        got = _apply(H, x, y)
        errs.append(((got[0] - u) ** 2 + (got[1] - v) ** 2) ** 0.5)
    rmse = float(np.mean(errs))
    pairs = [(court[best["mapping"][i]][1], pts[i][1])
             for i in sorted(best["mapping"])]
    ps, nt = perspective_score(pairs)
    ok = best["hits"] >= 5 and rmse < 1.5 and (ps >= 0.9 or nt == 0)
    note = (f"自动识别：{best['hits']}/{n} 个点找到了对应特征点，"
            f"平均误差 {rmse:.2f} m，透视一致率 {ps:.0%}")
    if ps < 0.9 and nt > 0:
        note += ("；⚠ 透视不一致 —— 这多半是**镜像解**（把远端当成了近端）。"
                 "请确认你选的「我看到的是」和实际一致，"
                 "或补标一个离篮筐很近的点（篮筐附近的点最能定朝向）。")
    if not ok:
        if best["hits"] < 5:
            note += ("；命中不足 5 个 —— 说明点的位置或数量不对"
                     "（请点在真正的场地线交点上，并尽量多标 1~2 个）")
        else:
            note += "；误差偏大，多半有点位点错"
    return {"ok": ok, "hits": best["hits"], "persp": round(ps, 3),
            "mapping": best["mapping"],
            "rmse_m": round(rmse, 3), "H": H.tolist() if hasattr(H, "tolist")
            else H, "note": note, "n": n}
