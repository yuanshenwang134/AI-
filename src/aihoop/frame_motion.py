"""镜头位移补偿：把不同帧上标的点，映射到同一个参考帧的坐标系。

为什么需要它（用户实测结论）：
  在同一段镜头内平移的几帧里，同一个球场特征点的**像素位置是不同的**；
  而标定要求所有点在同一坐标系里。实测证据：
    t=101.46s 的两个点误差 6.6m / 2.78m（同帧自洽），
    t=34.69s 的点误差 483m（跨镜头，完全对不上）。
  所以要先把各帧的点"搬"到同一帧的坐标系，再来解单应矩阵。

做法：
  1. 对相邻采样帧估计**背景单应矩阵**（ORB 特征匹配 + RANSAC）；
  2. 用内点率判断这两帧是否属于**同一镜头**（剪接处内点率会很低）；
  3. 同一镜头内的帧链式变换到参考帧；跨剪接的地方断开（分成不同 segment）；
  4. 每个 segment 内的点映射到该段第一帧，再解球场标定。

这个模块只做"几何"，不解球场标定 —— 职责单一，便于单独验证。
"""
from __future__ import annotations

from typing import Optional

import cv2
import numpy as np


def _grab(video: str, t: float, max_side: int = 960):
    cap = cv2.VideoCapture(str(video))
    if not cap.isOpened():
        return None, None
    fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
    cap.set(cv2.CAP_PROP_POS_FRAMES, max(0, int(round(t * fps))))
    ok, fr = cap.read()
    cap.release()
    if not ok:
        return None, None
    h, w = fr.shape[:2]
    scale = 1.0
    if max(h, w) > max_side:
        scale = max_side / float(max(h, w))
        fr = cv2.resize(fr, (int(w * scale), int(h * scale)),
                        interpolation=cv2.INTER_AREA)
    return fr, {"scale": scale, "w": w, "h": h, "fps": fps}


def estimate_motion(video: str, t_a: float, t_b: float,
                    min_matches: int = 12,
                    ok_ratio: float = 0.35) -> dict:
    """估计"把帧 b 的像素映到帧 a 的像素"的单应矩阵。

    返回 {ok, H, ratio, matches, note}；H 是 3x3（b→a）。
    ok=False 表示这两帧大概率**不在同一镜头**（剪接/大幅切换），不该硬连。
    """
    fa, ma = _grab(video, t_a)
    fb, mb = _grab(video, t_b)
    if fa is None or fb is None:
        return {"ok": False, "H": None, "ratio": 0.0, "matches": 0,
                "note": "取帧失败"}
    ga = cv2.cvtColor(fa, cv2.COLOR_BGR2GRAY)
    gb = cv2.cvtColor(fb, cv2.COLOR_BGR2GRAY)
    orb = cv2.ORB_create(nfeatures=1500)
    ka, da = orb.detectAndCompute(ga, None)
    kb, db = orb.detectAndCompute(gb, None)
    if da is None or db is None or len(ka) < min_matches or len(kb) < min_matches:
        return {"ok": False, "H": None, "ratio": 0.0, "matches": 0,
                "note": f"特征点太少（a={len(ka)}, b={len(kb)}）"}
    bf = cv2.BFMatcher(cv2.NORM_HAMMING, crossCheck=True)
    ms = bf.match(db, da)
    if len(ms) < min_matches:
        return {"ok": False, "H": None, "ratio": 0.0, "matches": len(ms),
                "note": f"匹配太少（{len(ms)}）"}
    ms = sorted(ms, key=lambda m: m.distance)[:300]
    src = np.float32([kb[m.queryIdx].pt for m in ms]).reshape(-1, 1, 2)  # b
    dst = np.float32([ka[m.trainIdx].pt for m in ms]).reshape(-1, 1, 2)  # a
    H, mask = cv2.findHomography(src, dst, cv2.RANSAC, 3.0)
    if H is None:
        return {"ok": False, "H": None, "ratio": 0.0, "matches": len(ms),
                "note": "RANSAC 没找到一致变换"}
    ratio = float(mask.sum()) / float(len(ms))
    # 缩放回原始分辨率坐标系（点是在缩放图上匹配的，这里把 H 还原）
    sc = ma["scale"]
    if sc != 1.0:
        S = np.array([[sc, 0, 0], [0, sc, 0], [0, 0, 1.0]])
        Si = np.linalg.inv(S)
        H = Si @ H @ S
    ok = ratio >= ok_ratio
    return {"ok": ok, "H": H.tolist(), "ratio": round(ratio, 3),
            "matches": len(ms),
            "note": ("同一镜头（内点率 %.0f%%）" % (ratio * 100)) if ok
                    else ("内点率只有 %.0f%% —— 大概率是剪接/换镜头"
                          % (ratio * 100))}


def _motion_via_chain(video: str, a: float, b: float,
                      step: float = 0.35) -> dict:
    """a→b 的变换：间隔大时**插入中间帧**逐段估计再串起来。

    为什么不能直接估：实测 57.5→58.4（相隔 0.9s）直接估只有 28% 内点，
    会被误判成剪接；而 56.3→56.9→57.5→58.1… 这样密集链式估计时全段内点率
    39~100%，判定正确。间隔越大、画面里人动得越多，直接估计越不可靠。
    """
    import numpy as _np
    if b - a <= step * 1.5:
        return estimate_motion(video, a, b)
    n = max(2, int(round((b - a) / step)))
    times = [a + (b - a) * i / n for i in range(n + 1)]
    H_total = _np.eye(3)
    ratios, notes = [], []
    for t0, t1 in zip(times, times[1:]):
        m = estimate_motion(video, t0, t1)
        ratios.append(m["ratio"])
        notes.append(m["note"])
        if not m["ok"] or m["H"] is None:
            return {"ok": False, "H": None, "ratio": round(min(ratios), 3),
                    "matches": m.get("matches", 0),
                    "note": (f"链式估计在 {t0:.1f}→{t1:.1f}s 断开（内点率 "
                             f"{m['ratio']:.0%}）——这两帧之间大概有剪接")}
        H_total = H_total @ _np.array(m["H"], dtype=_np.float64)
    return {"ok": True, "H": H_total.tolist(),
            "ratio": round(min(ratios), 3),
            "matches": 0,
            "note": (f"链式估计 {len(times)} 帧通过（最低内点率 "
                     f"{min(ratios):.0%}）")}


def build_segments(video: str, times: list[float],
                   ok_ratio: float = 0.35) -> dict:
    """把采样帧按"是否同一镜头"分组，并给出各帧到该段参考帧的变换。

    返回 {segments: [{times:[...], ref_t, to_ref:{t: H}}], motions:[...]}
    to_ref[t] 是把**帧 t 的像素**映到该段参考帧像素的 3x3 矩阵。
    """
    times = sorted(float(t) for t in times)
    motions = []
    segments = []
    cur = {"times": [times[0]], "ref_t": times[0], "to_ref": {times[0]: np.eye(3)}}
    for a, b in zip(times, times[1:]):
        m = _motion_via_chain(video, a, b)
        motions.append({"from": b, "to": a, **{k: v for k, v in m.items()
                                                if k != "H"},
                        "H": m.get("H")})
        if m["ok"] and m["H"] is not None:
            # 帧 b -> 帧 a，再链到参考帧
            Hab = np.array(m["H"], dtype=np.float64)
            Href_a = cur["to_ref"][a]
            cur["to_ref"][b] = Href_a @ Hab
            cur["times"].append(b)
        else:
            segments.append(cur)
            cur = {"times": [b], "ref_t": b, "to_ref": {b: np.eye(3)}}
    segments.append(cur)
    return {"segments": [{**s, "to_ref": {t: H.tolist()
                                          for t, H in s["to_ref"].items()}}
                         for s in segments],
            "motions": motions}


def warp_point(H, x: float, y: float) -> tuple[float, float]:
    """把一个像素点按 H 映射过去。"""
    v = np.array(H, dtype=np.float64) @ np.array([x, y, 1.0])
    return float(v[0] / v[2]), float(v[1] / v[2])
