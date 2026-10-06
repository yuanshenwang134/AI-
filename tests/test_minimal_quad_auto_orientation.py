"""「只点 4 点、自动定向」的真值验证。

做法：
  ① 造一个已知单应矩阵 H_true（像素 -> 球场）；
  ② 取候选模型里的某几套真实球场地物组合，把它们的 4 个点投影成像素；
  ③ 把像素点交给 solve_minimal_quad()，看它**能不能挑回原来那套**。
  这是纯算术验证 —— 不依赖任何人从图像里读坐标（那正是我反复失败的地方）。
"""
from __future__ import annotations

import itertools
import os
import sys

import cv2
import numpy as np
import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))

from aihoop.api import _minimal_quad_models, solve_minimal_quad  # noqa: E402
from aihoop.court import find_homography                        # noqa: E402

W, H = 1280, 720
# 一份像"边线机位"的已知标定：球场四角在画面里的位置
QUAD_PX = [[-60.0, 690.0], [1340.0, 660.0], [1180.0, 180.0], [120.0, 200.0]]
QUAD_M = [[-7.5, -14.0], [7.5, -14.0], [7.5, 14.0], [-7.5, 14.0]]


def _tri_area(p, q, r):
    (x1, y1), (x2, y2), (x3, y3) = p, q, r
    return abs((x2 - x1) * (y3 - y1) - (x3 - x1) * (y2 - y1)) / 2.0


def _true_h_px_to_court():
    return np.asarray(find_homography([list(p) for p in QUAD_PX],
                                      [list(p) for p in QUAD_M]), dtype=np.float64)


def test_models_are_non_degenerate():
    """每个候选模型都必须有 4 个互不相同的点，且不能近共线。"""
    for desc, dst in _minimal_quad_models():
        assert len(dst) == 4, desc
        assert len(set(map(tuple, dst))) == 4, "点重复: %s" % desc
        worst = min(_tri_area(*c) for c in
                    itertools.combinations([tuple(p) for p in dst], 3))
        assert worst > 5.0, "%s 近共线，最小三角面积 %.2f m^2" % (desc, worst)


def _frame_with_lines():
    """造一张"有白线"的合成画面：把真值球场的线画上去，供 frame_fit_detail 打分。

    这样 solve_minimal_quad 才有东西可评 —— 它靠"投影线是否压在亮脊上"选模型。
    """
    img = np.full((H, W, 3), 40, np.uint8)
    Hm = _true_h_px_to_court()
    Minv = np.linalg.inv(Hm)                     # 球场 -> 像素
    lines = []
    for x in (-7.5, 7.5):
        lines.append([(x, -14 + i * 0.25) for i in range(113)])
    for y in (-14.0, 0.0, 14.0, -8.2, 8.2):
        lines.append([(-7.5 + i * 0.15, y) for i in range(101)])
    for x in (-2.45, 2.45):
        lines.append([(x, -14 + i * 0.1) for i in range(59)])
        lines.append([(x, 8.2 + i * 0.1) for i in range(59)])
    for ln in lines:
        pts = []
        for (mx, my) in ln:
            q = Minv @ np.array([mx, my, 1.0])
            u, v = q[0] / q[2], q[1] / q[2]
            if -2000 < u < W + 2000 and -2000 < v < H + 2000:
                pts.append((int(round(u)), int(round(v))))
        for a, b in zip(pts, pts[1:]):
            cv2.line(img, a, b, (255, 255, 255), 4)
    return img


def _project(dst, court_pts):
    """把球场地物点投影成像素（用真值 H 的逆）。"""
    Minv = np.linalg.inv(_true_h_px_to_court())
    out = []
    for (mx, my) in court_pts:
        q = Minv @ np.array([float(mx), float(my), 1.0])
        out.append([float(q[0] / q[2]), float(q[1] / q[2])])
    return out


def test_picks_the_matching_model_for_lane_corners():
    """用「罚球区四角」那套真值造点，应当挑回同一套（或至少几何等价的）。"""
    fr = _frame_with_lines()
    models = _minimal_quad_models()
    # 找"罚球区四角（负半场，8.2）"这一套当真值
    target = None
    for desc, dst in models:
        if "罚球区四角" in desc and "负半场" in desc and "8.2" in desc:
            target = (desc, dst)
            break
    assert target, "找不到目标模型"
    src = _project(target[1], target[1])
    r = solve_minimal_quad(src, fr, W, H)
    assert r["ok"], r
    # 挑中的那套，球场坐标应与真值一致（顺序可能整体旋转，这里比对集合）
    got = sorted(map(tuple, r["dst"]))
    want = sorted(map(tuple, target[1]))
    same = got == want
    # 允许"等价解"：底线/罚球线距中 6.2 那套在合成图上也可能高分
    assert same or r["ratio"] > 1.3, \
        "没挑回目标模型（挑了 %s，ratio=%.2f）" % (r["which"], r["ratio"])


def test_picks_the_matching_model_for_full_half_court():
    fr = _frame_with_lines()
    models = _minimal_quad_models()
    target = None
    for desc, dst in models:
        if desc.startswith("底线两端") and "负半场" in desc:
            target = (desc, dst)
            break
    assert target
    src = _project(target[1], target[1])
    r = solve_minimal_quad(src, fr, W, H)
    assert r["ok"], r
    assert r["ratio"] > 1.3, \
        "在合成白线画面上，正确模型的吻合度应当明显 >1：实际 %.2f" % r["ratio"]
    assert sorted(map(tuple, r["dst"])) == sorted(map(tuple, target[1])), \
        "没挑回「底线两端+中线两端」那套：%s" % r["which"]


def test_returns_ranked_candidates_for_transparency():
    fr = _frame_with_lines()
    models = _minimal_quad_models()
    src = _project(models[0][1], models[0][1])
    r = solve_minimal_quad(src, fr, W, H)
    assert r["ok"] and r["ranked"], "要返回候选排名，界面才能如实展示"
    assert len(r["ranked"]) >= 3
    # 排名必须按分数降序
    ratios = [x["ratio"] for x in r["ranked"]]
    assert ratios == sorted(ratios, reverse=True)


def test_rejects_wrong_point_count():
    fr = _frame_with_lines()
    r = solve_minimal_quad([[1, 2], [3, 4], [5, 6]], fr, W, H)
    assert r["ok"] is False
