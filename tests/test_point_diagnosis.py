"""逐点责任诊断：把"哪个点跟其余对不上"钉住。

用户实测反馈（原话）："我标点就说表的不对，你这个标定的代码真的准确吗"。
所以这里同时钉两件事：

  A) **求解器本身必须准** —— 用 4 个角点回代，误差必须为 0；
     并与 numpy 独立 DLT 实现逐元素比对（若不一致就是求解器 bug）。
  B) **诊断要说人话** —— 6 个点里故意把"篮筐中心"改到一个明显错的位置，
     诊断必须把「篮筐中心」指出来，而且用**界面上的名字**。
"""
from __future__ import annotations

import os
import sys

import numpy as np
import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))

from aihoop.api import COURT_LANDMARKS, _diagnose_keypoints  # noqa: E402
from aihoop.court import apply_homography, find_homography  # noqa: E402

W, H = 854, 480


def np_dlt(src, dst):
    """独立的 DLT 最小二乘（归一化），用来交叉验证项目的实现。"""
    src = np.asarray(src, dtype=np.float64)
    dst = np.asarray(dst, dtype=np.float64)

    def norm(pts):
        c = pts.mean(axis=0)
        d = np.sqrt(((pts - c) ** 2).sum(axis=1)).mean()
        s = np.sqrt(2) / d if d > 0 else 1.0
        T = np.array([[s, 0, -s * c[0]], [0, s, -s * c[1]], [0, 0, 1.0]])
        ph = np.hstack([pts, np.ones((len(pts), 1))]) @ T.T
        return ph[:, :2], T

    sn, Ts = norm(src)
    dn, Td = norm(dst)
    A, b = [], []
    for (x, y), (u, v) in zip(sn, dn):
        A.append([x, y, 1, 0, 0, 0, -u * x, -u * y]); b.append(u)
        A.append([0, 0, 0, x, y, 1, -v * x, -v * y]); b.append(v)
    h, *_ = np.linalg.lstsq(np.asarray(A), np.asarray(b), rcond=None)
    Hn = np.array([[h[0], h[1], h[2]], [h[3], h[4], h[5]], [h[6], h[7], 1.0]])
    Hm = np.linalg.inv(Td) @ Hn @ Ts
    return Hm / Hm[2, 2]


def test_solver_maps_corners_exactly():
    """4 个角点：回代必须精确到 0（这是"求解器准不准"最直接的证据）。"""
    pix = [[-80.0, 150.0], [910.0, 115.0], [930.0, 530.0], [-95.0, 515.0]]
    m = [[-7.5, 0.0], [7.5, 0.0], [7.5, -14.0], [-7.5, -14.0]]
    Hm = find_homography(pix, m)
    for p, want in zip(pix, m):
        got = apply_homography(Hm, p[0], p[1])
        assert abs(got[0] - want[0]) < 1e-6 and abs(got[1] - want[1]) < 1e-6, \
            "角点回代不精确：%s -> %s（期望 %s）" % (p, got, want)


def test_solver_agrees_with_numpy_dlt():
    """与 numpy 独立实现比对：不一致就说明求解器有 bug。"""
    pix = [[-80.0, 150.0], [910.0, 115.0], [930.0, 530.0], [-95.0, 515.0],
           [383.0, 176.0], [224.0, 294.0], [550.0, 289.0]]
    m = [[-7.5, 0.0], [7.5, 0.0], [7.5, -14.0], [-7.5, -14.0],
         [0.0, -1.575], [-2.45, -5.8], [2.45, -5.8]]
    Hs = np.asarray(find_homography(pix, m))
    Hn = np_dlt(pix, m)
    assert np.allclose(Hs, Hn, atol=1e-9), \
        "与 numpy DLT 不一致：\n项目=%s\nnumpy=%s" % (Hs, Hn)


def test_diagnosis_points_at_the_bad_point():
    """故意把「篮筐中心」放到明显错的位置，诊断必须点它的名。"""
    good = {"lane_far_left": (0.293, 0.617), "lane_far_right": (0.548, 0.617),
            "lane_near_left": (0.325, 0.793), "lane_near_right": (0.603, 0.793)}
    # 用一份"正常"的 H 造出这几个点的像素，再加一个明显错的篮筐点
    Htrue = find_homography([[0, 0], [854, 0], [854, 480], [0, 480]],
                            [[-7.5, 0.0], [7.5, 0.0], [7.5, -14.0], [-7.5, -14.0]])
    src, dst, tags = [], [], []
    for k, (x, y) in good.items():
        src.append([x * W, y * H])
        dst.append(list(COURT_LANDMARKS[k]))
        tags.append({"name": k, "label": k, "t": 1.0})
    src.append([300.0, 150.0])                       # 故意错：篮筐不该在这
    dst.append(list(COURT_LANDMARKS["hoop_far"]))
    tags.append({"name": "hoop_far", "label": "篮筐中心", "t": 1.0})

    d = _diagnose_keypoints(src, dst, tags, label_of={"hoop_far": "篮筐中心"})
    assert d["enough"] is True
    assert d["worst"] is not None
    assert d["worst"]["label"] == "篮筐中心", \
        "诊断必须点名「篮筐中心」，实际点了 %r" % d["worst"]["label"]
    assert d["worst"]["excused_err_m"] > 1.0, \
        "责任点的偏差应当显著：%s" % d["worst"]["excused_err_m"]


def _six_points():
    pts = {"hoop_near": (0.573, 0.320), "corner_near_left": (0.470, 0.474),
           "lane_near_left": (0.637, 0.632), "ft_near": (0.556, 0.616),
           "lane_near_right": (0.506, 0.616), "arc_near": (0.357, 0.640)}
    src = [[x * W, y * H] for (x, y) in pts.values()]
    dst = [list(COURT_LANDMARKS[k]) for k in pts]
    return pts, src, dst


def test_diagnosis_prefers_the_label_carried_by_the_point():
    """点自带的 label 就是界面上的名字 —— 诊断必须原样用它。

    （后端把某一侧的篮筐叫「另一端的篮筐中心」，照抄后端名字用户对不上按钮。）
    """
    pts, src, dst = _six_points()
    tags = [{"name": k, "label": "界面名-%d" % i, "t": 1.0}
            for i, k in enumerate(pts)]
    d = _diagnose_keypoints(src, dst, tags)
    got = [r["label"] for r in d["rows"]]
    assert all(lbl.startswith("界面名-") for lbl in got), \
        "应当用点自带的 label（界面名），实际 %s" % got


def test_diagnosis_falls_back_to_label_map_when_point_has_none():
    """点上没有 label 时（老格式），用调用方给的映射，最后才用后端措辞。"""
    pts, src, dst = _six_points()
    tags = [{"name": k, "t": 1.0} for k in pts]          # 故意不带 label
    ui = {k: "界面名-%d" % i for i, k in enumerate(pts)}
    d = _diagnose_keypoints(src, dst, tags, label_of=ui)
    got = [r["label"] for r in d["rows"]]
    assert all(lbl.startswith("界面名-") for lbl in got), \
        "应当回退到 label_of 映射，实际 %s" % got


def test_diagnosis_needs_at_least_five_points():
    """4 个点时误差恒为 0，诊断没有判别力 —— 必须如实说，而不是硬给结论。"""
    src = [[0, 0], [854, 0], [854, 480], [0, 480]]
    dst = [[-7.5, 0.0], [7.5, 0.0], [7.5, -14.0], [-7.5, -14.0]]
    tags = [{"name": "a", "label": "a", "t": 1.0} for _ in range(4)]
    d = _diagnose_keypoints(src, dst, tags)
    assert d["enough"] is False
    assert "5" in d["note"]
