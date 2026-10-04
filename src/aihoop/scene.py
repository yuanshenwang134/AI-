"""场景切镜检测。

用来把「同一段连续镜头」和「切镜后的另一段镜头」分开。
篮筐/篮球轨迹都不应该跨切镜点连接，否则会把两个镜头里的物体连成一条轨迹。

判据：
  * 相邻采样帧的平均绝对差（MAD）突然变大；
  * 同时 HSV 色相直方图相关性骤降。
两个条件都满足才认为是切镜，避免把闪光、快速运动误判成切镜。
"""
from __future__ import annotations

from typing import Optional


def detect_scene_cuts(video_path: str,
                      stride: int = 3,
                      hist_thr: float = 0.55,
                      mad_thr: float = 25.0) -> list[float]:
    """返回切镜时刻（秒）列表。"""
    try:
        import cv2  # noqa
        import numpy as np
    except ImportError:
        return []
    cap = cv2.VideoCapture(video_path)
    if not cap.isOpened():
        return []
    fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
    cuts: list[float] = []
    prev_hist = None
    prev_gray = None
    idx = 0
    step = max(1, int(stride))
    while True:
        ok = cap.grab()
        if not ok:
            break
        if idx % step:
            idx += 1
            continue
        ok, frame = cap.retrieve()
        if not ok:
            break
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        hsv = cv2.cvtColor(frame, cv2.COLOR_BGR2HSV)
        hist = cv2.calcHist([hsv], [0], None, [180], [0, 180])
        cv2.normalize(hist, hist)
        if prev_hist is not None and prev_gray is not None:
            corr = float(cv2.compareHist(prev_hist, hist, cv2.HISTCMP_CORREL))
            mad = float(cv2.absdiff(gray, prev_gray).mean())
            if corr < hist_thr and mad > mad_thr:
                cuts.append(round(idx / fps, 3))
        prev_hist = hist
        prev_gray = gray
        idx += 1
    cap.release()
    return cuts
