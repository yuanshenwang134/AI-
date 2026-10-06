"""自动逐帧标定给出的 H，不能离谱到不可能是球场。

背景（用户实测 2026-10-06）：
  那份 job 里 sliding 自称 median_ratio=10.96、达标率 1.000，看起来很健康，
  但它把**画面四角**投进去得到 (7.5, -76.9) 这类值 —— y 跨度 71m（球场才 28m）。
  球员坐标因此有 89% 被压到边线 x=7.5 上，战术图"人都挤在底线"。

  注意这个判据**区分能力有限**：实测"良好"的标定也能投出 |y|max 55.9，
  所以阈值必须定得很宽（只拦 |x|>40 / |y|>60 这种极端值）。
  真正兜底的是 `_tactics_coords_sane`（看坐标分布，见另一个测试文件）。
"""
from __future__ import annotations

import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))

from aihoop.pipeline import _sliding_homography_sane  # noqa: E402

BASE = {"fps": 30.0, "total_frames": 5521, "half_court": True,
        "median_ratio": 10.96, "ok_ratio": 1.0}

GOOD_T60 = [[-81.9, 148.9], [910.0, 113.3], [933.2, 527.0], [-97.2, 515.2]]
GOOD_T5 = [[30.2, 270.5], [668.9, 138.7], [745.3, 430.6], [-60.6, 458.6]]
BAD_T0 = [[311.33, 401.90], [860.62, 202.90], [882.16, 517.97], [327.43, 391.63]]


def _meta(corners):
    return {"width": 854, "height": 480, "sliding_anchors": 1080,
            "sliding_calibration": dict(BASE, anchors=[
                {"frame": 0, "corners": corners, "ratio": 10.0}])}


def test_rejects_degenerate_anchor():
    """事故那次的四角 —— 必须拦下。"""
    ok, why = _sliding_homography_sane(_meta(BAD_T0))
    assert ok is False, "退化标定必须被拦下"
    assert "离谱" in why or "球场" in why, why


def test_accepts_normal_camera_view():
    """正常视野的标定不能误杀（两次实测都通过）。"""
    for tag, quad in (("auto t=60s", GOOD_T60), ("t=5s", GOOD_T5)):
        ok, why = _sliding_homography_sane(_meta(quad))
        assert ok is True, "%s 被误杀：%s" % (tag, why)


def test_no_evidence_never_blocks():
    """没有锚点样本（老产物只有统计量）时不许判负 —— 没有证据就不拦。"""
    ok, why = _sliding_homography_sane(
        {"width": 854, "height": 480, "sliding_anchors": 1080,
         "sliding_calibration": BASE})
    assert ok is True and why == ""


def test_no_sliding_at_all_never_blocks():
    ok, why = _sliding_homography_sane({})
    assert ok is True and why == ""


def test_missing_size_never_blocks():
    ok, why = _sliding_homography_sane({"sliding_anchors": 5,
                                        "sliding_calibration": dict(BASE, anchors=[
                                            {"frame": 0, "corners": BAD_T0}])})
    assert ok is True and why == "", "拿不到画面尺寸时不判"


def test_sources_keeps_anchor_samples():
    """sources 往 meta 里存 sliding 结果时，必须留几个锚点样本。

    只存统计量的话，pipeline 里就没法验证 H 的合理性（实测踩到：
    锚点被剔了，合理性检查只能干看着坏标定往下走）。
    """
    import io
    p = os.path.join(ROOT, "src", "aihoop", "sources.py")
    src = io.open(p, encoding="utf-8").read()
    assert "anchors_truncated" in src, "应当在 meta 里标注锚点被截断"
    assert 'for kk in ("frame", "corners", "ratio", "kind")' in src, \
        "应当保留 frame/corners/ratio 这几个字段的锚点样本"
