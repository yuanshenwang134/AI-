"""镜头位移补偿的单元测试（纯函数，不写临时文件 —— 沙箱里 tmp_path 不可写）。

覆盖：
  * `warp_point` 的数学（平移变换必须精确抵消）；
  * `estimate_motion` 在拿不到视频时**干净失败**而不是抛异常；
  * `build_segments` 的空输入不崩。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from aihoop.frame_motion import (build_segments, estimate_motion,  # noqa: E402
                                 warp_point)


def test_warp_point_translation():
    """平移变换：映射结果应精确等于坐标减去位移。"""
    H = [[1.0, 0.0, -35.0], [0.0, 1.0, 18.0], [0.0, 0.0, 1.0]]
    for (x, y) in [(200.0, 150.0), (0.0, 0.0), (639.5, 359.5)]:
        wx, wy = warp_point(H, x, y)
        assert abs(wx - (x - 35.0)) < 1e-9
        assert abs(wy - (y + 18.0)) < 1e-9


def test_warp_point_identity():
    wx, wy = warp_point([[1, 0, 0], [0, 1, 0], [0, 0, 1]], 12.5, 34.5)
    assert (round(wx, 6), round(wy, 6)) == (12.5, 34.5)


def test_estimate_motion_missing_video_is_clean_failure():
    """拿不到视频时必须 ok=False，而不是抛异常（上游要能继续跑）。"""
    m = estimate_motion("__no_such_video__.mp4", 0.0, 1.0)
    assert m["ok"] is False
    assert m["H"] is None
    assert m["ratio"] == 0.0
    assert isinstance(m["note"], str) and m["note"]


def test_build_segments_single_time():
    """只有一个时刻时不崩，且该帧就是参考帧、变换是单位矩阵。"""
    out = build_segments("__no_such_video__.mp4", [3.5])
    assert len(out["segments"]) == 1
    seg = out["segments"][0]
    assert seg["ref_t"] == 3.5
    assert seg["times"] == [3.5]
    H = seg["to_ref"][3.5]
    assert H[0][0] == pytest.approx(1.0) and H[0][2] == pytest.approx(0.0)
