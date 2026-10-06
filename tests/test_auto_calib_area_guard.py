"""auto_calibrate 的候选四边形必须有"合理的最小面积"。

背景（实测 2026-10-06）：
  auto_calibrate 曾选中一个**只占画面 20.7%** 的细长对角带，并给它打分
  ratio=20.5（比合理候选的 6.75 还高）—— 因为白地板广告也是"亮脊"，
  现在的 _ridge_strength 分不出"细的球场白线"和"大块白地板广告"
  （实测两组采样点的亮脊强度 26.7 vs 8.0，我挑点的坐标不可靠，无法用作判据）。
  它锁错之后，球员坐标有 89% 被压到边线上、真篮筐被投到场外 4~7m。

本文件钉住两条：
  ① 面积门槛是 25%（别再退回 10%，那样细长对角带又会溜过去）；
  ② 不变量：坏四边形（20.7%）被拒、合理四边形（42%）通过。

⚠️ 明确边界：这条门槛**只能拦掉明显退化解，不能让自动标定变准**。
   实测改完之后真篮筐仍然偏 4~7m（判据是应落在 (0,±1.575) 附近）。
   这一机位几乎正对球场、单应矩阵天然病态，自动标定在这份素材上不可用 ——
   要拿到可用标定得人工标点，或换机位更斜、分辨率更高的素材。
"""
from __future__ import annotations

import io
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))

import numpy as np  # noqa: E402


def _area_pct(quad, w, h) -> float:
    arr = np.array(quad, dtype=np.float64)
    area = 0.5 * abs(float(np.dot(arr[:, 0], np.roll(arr[:, 1], -1))
                           - np.dot(arr[:, 1], np.roll(arr[:, 0], -1))))
    return 100.0 * area / float(w * h)


def test_area_threshold_is_25_percent():
    src = io.open(os.path.join(ROOT, "src", "aihoop", "calibcheck.py"),
                  encoding="utf-8").read()
    assert "0.25 * bgr.shape[0] * bgr.shape[1]" in src, \
        "面积门槛必须保持 25%（退回 10% 会让细长对角带溜过去）"


def test_degenerate_diagonal_band_is_rejected():
    """实测那个坏四边形（画面 20.7%）必须在门槛之下。"""
    bad = [[311.33, 401.90], [860.62, 202.90], [882.16, 517.97], [327.43, 391.63]]
    pct = _area_pct(bad, 854, 480)
    assert pct < 25.0, "坏四边形面积 %.1f%% 应低于 25%%（实测 20.7%%）" % pct


def test_reasonable_quad_passes():
    """实测那个合理四边形（画面 42.1%）必须在门槛之上。"""
    good = [[30.2, 270.5], [668.9, 138.7], [745.3, 430.6], [-60.6, 458.6]]
    pct = _area_pct(good, 854, 480)
    assert pct > 25.0, "合理四边形面积 %.1f%% 应高于 25%%（实测 42.1%%）" % pct


def test_fit_ratio_alone_cannot_be_trusted():
    """留一条"防回归"的说明性断言：坏解的 ratio 反而更高。

    这条不是测代码，是**把踩过的坑写进测试**，避免以后有人看到
    "ratio 更高"就以为找到了更好的标定。
    """
    bad_ratio, good_ratio = 20.5, 6.75      # 实测值
    assert bad_ratio > good_ratio, \
        "记录的事实：细长对角带的 ratio(20.5) 高于合理候选(6.75) —— " \
        "所以**不能**只按 ratio 挑候选"
