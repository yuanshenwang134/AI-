"""多画面标定：**用哪一幅画面解算**的选择规则（前后端"左右两个画面"契约）。

背景（用户实测）：一个镜头里只看得见半场，想在一个画面里点完所有特征点根本点不全。
界面因此改成**左右两个画面各自标一个半场**。这里钉住后端对应的两条规则：

  1. 只要有一幅画面自己够 4 个点，就用那一幅解（取点多的一幅），
     **不**把两幅的点揉成一份 H —— 两台机位混拟合会解出"谁都不对"的折中解；
  2. 一幅都没凑够 4 个点时，才跨画面合并（并靠镜头位移补偿）；
  3. "矛盾点"必须按**真正用到的那份 H** 重算，不能拿被丢掉的合并解的离群点去提示用户。

这里刻意用**纯 python** 的 find_homography（court.py 有无 numpy 兜底实现），
所以本文件不依赖 opencv。
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), "src"))

from aihoop.api import _pick_calibration_homography       # noqa: E402
from aihoop.court import apply_homography, find_homography  # noqa: E402

# 一份"像素 -> 球场"的假单应矩阵（纯尺度+平移，数值可控）
H_TRUE = [[0.01, 0.0, -4.0],
          [0.0, 0.01, -2.5],
          [0.0, 0.0, 1.0]]


def _frame_points(t, names, px0):
    """造一幅画面的点：像素坐标铺开（**不共线**），球场坐标由 H_TRUE 反算（自洽）。"""
    offs = [(0.0, 0.0), (40.0, 10.0), (10.0, 35.0), (55.0, 60.0),
            (80.0, 15.0), (25.0, 85.0)]
    src, dst, tags = [], [], []
    for i, name in enumerate(names):
        dx, dy = offs[i % len(offs)]
        x = px0 + dx
        y = 100.0 + dy
        u, v = apply_homography(H_TRUE, x, y)
        src.append([x, y])
        dst.append([u, v])
        tags.append({"name": name, "label": name, "t": t})
    return src, dst, tags


def _per_frame(t, src, dst):
    Hf = find_homography(src, dst)
    err = 0.0
    for (x, y), (u, v) in zip(src, dst):
        px, py = apply_homography(Hf, x, y)
        err += ((px - u) ** 2 + (py - v) ** 2) ** 0.5
    err /= max(1, len(src))
    return {"t": t, "n": len(src), "ok": err < 1.5, "rmse_m": round(err, 3),
            "H": Hf if err < 1.5 else None}


def test_single_frame_wins_over_merge():
    """两幅画面各自都够 4 个点 → 用点多的那一幅，不做跨画面混拟合。"""
    names_a = ["a1", "a2", "a3", "a4", "a5"]
    src_a, dst_a, tags_a = _frame_points(10.0, names_a, 100.0)
    names_b = ["b1", "b2", "b3", "b4"]
    src_b, dst_b, tags_b = _frame_points(40.0, names_b, 400.0)
    src, dst, tags = src_a + src_b, dst_a + dst_b, tags_a + tags_b
    fits = [_per_frame(10.0, src_a, dst_a), _per_frame(40.0, src_b, dst_b)]

    merged_h = find_homography(src, dst)
    merged_rmse = 9.9                       # 故意给一个"看着还行"的合并误差
    out = _pick_calibration_homography(merged_h, merged_rmse, src, dst, tags, fits)

    assert out["via"] == "single-frame"
    assert out["t"] == 10.0                 # 点多的那一幅（5 个点）
    assert out["H"] == fits[0]["H"]
    assert len(out["src"]) == len(names_a)
    # 送进来的点各自都在自己那一组里
    assert all(t["t"] == 10.0 for t in out["tags"])
    assert out["rmse"] == fits[0]["rmse_m"]
    assert "t=10.00s" in out["note"]


def test_merge_kept_when_no_frame_alone_is_enough():
    """一幅都没凑够 4 个点 → 保持跨画面合并的结果。"""
    src_a, dst_a, tags_a = _frame_points(10.0, ["a1", "a2", "a3"], 100.0)
    src_b, dst_b, tags_b = _frame_points(40.0, ["b1", "b2", "b3"], 400.0)
    src, dst, tags = src_a + src_b, dst_a + dst_b, tags_a + tags_b
    fits = [{"t": 10.0, "n": 3, "ok": False, "rmse_m": None, "H": None},
            {"t": 40.0, "n": 3, "ok": False, "rmse_m": None, "H": None}]
    merged_h = find_homography(src, dst)

    out = _pick_calibration_homography(merged_h, 0.4, src, dst, tags, fits)
    assert out["via"] == "merged"
    assert out["H"] == merged_h
    assert len(out["src"]) == 6             # 合并仍然用上全部点
    assert out["t"] is None
    assert out["outliers"] is None          # 不动外部已有的离群点结论


def test_outliers_recomputed_with_the_chosen_h():
    """选了单幅画面后，"矛盾点"必须按这一份 H 重算。

    重点是：另一幅画面里那个"错点"不该再被报出来 —— 它根本没参与解算。
    触发条件必须写对：只有当**合并解站不住**（内点太少）时才会改用单幅
    （合并解很好的时候用合并解更稳，见 test_merged_kept_when_merge_is_solid）。
    """
    names_a = ["a1", "a2", "a3", "a4", "a5"]
    src_a, dst_a, tags_a = _frame_points(10.0, names_a, 100.0)
    names_b = ["b1", "b2", "b3", "b4"]
    src_b, dst_b, tags_b = _frame_points(40.0, names_b, 400.0)
    src, dst, tags = src_a + src_b, dst_a + dst_b, tags_a + tags_b
    fits = [_per_frame(10.0, src_a, dst_a), _per_frame(40.0, src_b, dst_b)]

    merged_h = find_homography(src, dst)
    out = _pick_calibration_homography(merged_h, 0.5, src, dst, tags, fits,
                                      merged_inliers=2)
    assert out["via"] == "single-frame" and out["t"] == 10.0
    # 自洽的那一幅：5 个点全是内点，没有"矛盾点"
    assert out["outliers"] == []
    assert out["n_inliers"] == 5


def test_merged_kept_when_merge_is_solid():
    """合并解本身很好（内点 ≥5、平均误差 <0.75m）→ 就用合并解，别乱换。"""
    names_a = ["a1", "a2", "a3", "a4", "a5"]
    src_a, dst_a, tags_a = _frame_points(10.0, names_a, 100.0)
    names_b = ["b1", "b2", "b3", "b4"]
    src_b, dst_b, tags_b = _frame_points(40.0, names_b, 400.0)
    src, dst, tags = src_a + src_b, dst_a + dst_b, tags_a + tags_b
    fits = [_per_frame(10.0, src_a, dst_a), _per_frame(40.0, src_b, dst_b)]
    merged_h = find_homography(src, dst)

    out = _pick_calibration_homography(merged_h, 0.30, src, dst, tags, fits,
                                      merged_inliers=9)
    assert out["via"] == "merged"
    assert out["H"] == merged_h
    assert len(out["src"]) == 9


def test_single_frame_wins_when_only_four_inliers():
    """只有 4 个内点时**不能**信合并解：那 4 个点本身就精确决定 H，误差必然很小。

    这正是用户实测踩到的坏情况：10 个点里 4 个被判矛盾、只剩 1 个内点，
    界面还在用这份 H，同时又说"t=4.75s 单帧 5 点 1.413m 可解" —— 两个结论打架。
    """
    names_a = ["a1", "a2", "a3", "a4", "a5"]
    src_a, dst_a, tags_a = _frame_points(10.0, names_a, 100.0)
    names_b = ["b1", "b2", "b3", "b4"]
    src_b, dst_b, tags_b = _frame_points(40.0, names_b, 400.0)
    src, dst, tags = src_a + src_b, dst_a + dst_b, tags_a + tags_b
    fits = [_per_frame(10.0, src_a, dst_a), _per_frame(40.0, src_b, dst_b)]
    merged_h = find_homography(src, dst)

    out = _pick_calibration_homography(merged_h, 0.60, src, dst, tags, fits,
                                      merged_inliers=4)
    assert out["via"] == "single-frame"


def test_pick_helper_is_pure():
    """不该改动传进来的列表（调用方后面还要用完整点集）。"""
    src, dst, tags = _frame_points(10.0, ["a1", "a2", "a3", "a4"], 100.0)
    fits = [_per_frame(10.0, src, dst)]
    before = (len(src), len(dst), len(tags))
    _pick_calibration_homography(find_homography(src, dst), 0.2, src, dst, tags, fits)
    assert (len(src), len(dst), len(tags)) == before
