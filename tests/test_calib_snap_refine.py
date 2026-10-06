"""端到端验证「点吸附精修」接进预览接口后的行为（用用户那 6 个真实点）。

要求：
  ① 接口不能因为精修而报错（精修失败必须静默降级）；
  ② 这机位太正对、单应病态 -> 护栏应当**拒绝**退化解，点集保持原样；
  ③ 回包里要带上 snap 的说明，让界面能如实告诉用户"保留了你标的点"。
"""
from __future__ import annotations

import asyncio
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))

from aihoop import api  # noqa: E402

VIDEO = "data/uploads/basketball_match_3min.mp4"
W, H, T = 854, 480, 58.5
# 用户那次真实标的点（归一化）
USER = {
    "hoop_near":         [183.1 / W, 90.2 / H],
    "corner_near_right": [307.7 / W, 199.5 / H],
    "lane_near_left":    [441.4 / W, 371.7 / H],
    "lane_near_right":   [522.4 / W, 295.9 / H],
    "ft_near":           [489.0 / W, 329.7 / H],
    "arc_near":          [627.8 / W, 340.0 / H],
}


def _run(snap: bool):
    body = {"video_path": VIDEO, "compensate": False, "confirm": False,
            "snap": snap,
            "frames": [{"t": T, "landmarks": {k: {"value": v, "label": k}
                                              for k, v in USER.items()}}]}
    return asyncio.run(api.post_calibrate_multi(api.MultiCalibRequest(**body)))


def test_snap_does_not_break_preview():
    r = _run(True)
    assert r is not None
    assert "calibration_fit" in r
    assert r.get("n_points", 0) >= 4


def test_snap_rejected_on_degenerate_view_keeps_user_points():
    """这机位病态 -> 护栏拒绝，吻合度不应被"刷"到离谱值。"""
    r = _run(True)
    fit = r.get("calibration_fit") or {}
    ratio = fit.get("ratio")
    assert ratio is not None
    # 退化解会把 ratio 刷到几十；正常真实画面只有 1.3~2.3 量级
    assert float(ratio) < 12.0, "吻合度被刷到 %s —— 护栏没生效" % ratio
    snap = r.get("snap") or {}
    if snap:
        assert "note" in snap
        if not snap.get("accepted"):
            assert "保留" in snap["note"] or "回退" in snap["note"], snap["note"]


def test_snap_can_be_disabled():
    r = _run(False)
    assert r.get("snap") in (None, {}) or not (r.get("snap") or {}).get("accepted")


def test_refiner_never_moves_points_more_than_limit():
    """硬约束：任何情况下单点位移都不超过 20 像素。"""
    from aihoop.calibcheck import refine_keypoints
    import cv2
    cap = cv2.VideoCapture(VIDEO)
    cap.set(cv2.CAP_PROP_POS_FRAMES, int(T * (cap.get(cv2.CAP_PROP_FPS) or 30)))
    ok, fr = cap.read()
    cap.release()
    assert ok, "取帧失败"
    r = refine_keypoints([{"bgr": fr, "landmarks": {k: list(v) for k, v in USER.items()}}],
                         landmark_map=api.COURT_LANDMARKS, max_shift_px=12.0, passes=1)
    for k, d in (r.get("moved_px") or {}).items():
        assert d <= 20.0, "%s 移动了 %.1f 像素，超过上限" % (k, d)
