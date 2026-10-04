"""逐帧相机跟踪 —— 让俯视战术图能用在**会动的镜头**上。

为什么必须有这一层
------------------------------------------------------------------
单应标定（拍一张、标一次、整场复用）只对固定机位成立。真实素材里手机手持、
导播摇摄、推拉变焦非常常见（实测：一段 5.8s 手机片段每秒位移 10px，
一段广播素材 4px/s 且带切镜）。这时候拿同一个 H 去投球员，位置会整体漂移，
**而且画出来依然像一张战术图，不会报错**。

做法（不需要模型、不需要 GPU）
------------------------------------------------------------------
以"标定那一刻的帧"为**锚点帧**，用 ORB 特征把每一帧配准回锚点帧，得到
M（当前帧像素 -> 锚点帧像素），再与锚点帧的标定 H 复合：

    当前帧像素 --M--> 锚点帧像素 --H--> 球场坐标        =>  H_now = H_cal ∘ M

**每一帧都直接对锚点帧配准，而不是对上一帧链式累加** —— 这是最关键的设计
选择：链式累加跑几分钟必然漂到天上，而对锚点直接配准的误差**不随时间增长**，
只在锚点特征看不见时才失效（此时宁可丢帧也不硬投）。

失效处理：内点不足 -> ok=False -> 调用方跳过该帧的投影。缺几帧还能看，
错位的战术图会误导人。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

import numpy as np

# 配准参数（都是"能讲出道理"的经验值，不是调出来的玄学）
ORB_FEATURES = 3000      # 720p 画面用 3000 个特征足够覆盖球场线/观众席
RATIO_TEST = 0.75        # Lowe ratio：最像的必须明显比第二像的更像
MIN_MATCHES = 14         # 少于这个数不认为配准成功
MIN_RATIO = 0.25         # RANSAC 内点率下限
RANSAC_REPROJ = 3.0      # px，内点阈值


def _require_cv():
    try:
        import cv2  # noqa: F401
    except ImportError as e:  # pragma: no cover
        raise RuntimeError(
            "逐帧相机跟踪需要 opencv-python。安装："
            "pip install -r requirements-full.txt") from e


@dataclass
class AnchorFrame:
    """锚点帧：标定 H 生效的那一帧。"""
    time: float
    size: tuple                    # (w, h)
    H_pixel_to_court: list


class CameraTracker:
    """把每一帧配准回锚点帧，给出「这一帧的 像素 -> 球场 单应」。"""

    def __init__(self, anchor: AnchorFrame):
        _require_cv()
        import cv2
        self.cv2 = cv2
        self.anchor = anchor
        self._orb = cv2.ORB_create(nfeatures=ORB_FEATURES)
        self._matcher = cv2.BFMatcher(cv2.NORM_HAMMING, crossCheck=False)
        self._kp0 = None
        self._desc0 = None
        self.H_cal = np.array(anchor.H_pixel_to_court, dtype=np.float64)

    # ---------------- 锚点 ----------------
    def set_anchor_image(self, bgr) -> int:
        """用锚点帧图像预提特征（只算一次）。返回特征数。"""
        gray = self.cv2.cvtColor(bgr, self.cv2.COLOR_BGR2GRAY)
        self._kp0, self._desc0 = self._orb.detectAndCompute(gray, None)
        return 0 if self._desc0 is None else len(self._desc0)

    @property
    def anchor_features(self) -> int:
        return 0 if self._desc0 is None else len(self._desc0)

    # ---------------- 逐帧 ----------------
    def locate(self, bgr) -> dict:
        """估计这一帧的 像素->球场 单应。"""
        cv2 = self.cv2
        if self._desc0 is None or len(self._desc0) < MIN_MATCHES:
            return {"ok": False, "H": None, "inliers": 0, "matches": 0,
                    "ratio": 0.0, "note": "锚点帧可选特征太少（画面纹理不足）"}
        gray = cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY)
        kp, desc = self._orb.detectAndCompute(gray, None)
        if desc is None or len(desc) < 8:
            return {"ok": False, "H": None, "inliers": 0, "matches": 0,
                    "ratio": 0.0, "note": "当前帧特征不足"}
        knn = self._matcher.knnMatch(desc, self._desc0, k=2)
        good = [p[0] for p in knn
                if len(p) == 2 and p[0].distance < RATIO_TEST * p[1].distance]
        if len(good) < MIN_MATCHES:
            return {"ok": False, "H": None, "inliers": 0, "matches": len(good),
                    "ratio": 0.0,
                    "note": f"与锚点帧匹配点太少（{len(good)}）：镜头移开或切镜"}
        src = np.float32([kp[m.queryIdx].pt for m in good])       # 当前帧像素
        dst = np.float32([self._kp0[m.trainIdx].pt for m in good])  # 锚点帧像素
        M, mask = cv2.findHomography(src, dst, cv2.RANSAC, RANSAC_REPROJ)
        if M is None:
            return {"ok": False, "H": None, "inliers": 0, "matches": len(good),
                    "ratio": 0.0, "note": "单应求解失败"}
        inl = int(mask.sum()) if mask is not None else 0
        ratio = inl / max(1, len(good))
        if inl < MIN_MATCHES or ratio < MIN_RATIO:
            return {"ok": False, "H": None, "inliers": inl, "matches": len(good),
                    "ratio": round(ratio, 3),
                    "note": f"内点不足（{inl}/{len(good)}）"}
        # M: 当前帧 -> 锚点帧；H_cal: 锚点帧 -> 球场
        H_now = self.H_cal @ M
        return {"ok": True, "H": H_now.tolist(), "inliers": inl,
                "matches": len(good), "ratio": round(ratio, 3), "note": ""}

    # ---------------- 便捷：像素 -> 球场（带安全阀） ----------------
    def project(self, bgr, x_px: float, y_px: float
                ) -> tuple[Optional[tuple[float, float]], dict]:
        """把当前帧里的一个像素点投到球场坐标；配准失败时返回 (None, info)。"""
        r = self.locate(bgr)
        if not r["ok"]:
            return None, r
        H = np.array(r["H"], dtype=np.float64)
        d = H[2, 0] * x_px + H[2, 1] * y_px + H[2, 2]
        if abs(d) < 1e-12:
            return None, r
        x = (H[0, 0] * x_px + H[0, 1] * y_px + H[0, 2]) / d
        y = (H[1, 0] * x_px + H[1, 1] * y_px + H[1, 2]) / d
        # 这里返回的是**标定输出坐标**（可能是未折半的），由调用方按 cal 折半
        return (float(x), float(y)), r


def make_anchor(video_path: str, cal, time: float = 0.0
                ) -> tuple["CameraTracker", dict]:
    """从视频的某个时刻建锚点（该时刻必须已有可用标定）。"""
    _require_cv()
    import cv2
    if not getattr(cal, "H", None):
        raise RuntimeError("标定里没有 H，无法作为锚点")
    cap = cv2.VideoCapture(video_path)
    fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
    cap.set(cv2.CAP_PROP_POS_FRAMES, int(time * fps))
    ok, frame = cap.read()
    cap.release()
    if not ok:
        raise RuntimeError(f"读不到锚点帧：t={time}s")
    anchor = AnchorFrame(time=time, size=(frame.shape[1], frame.shape[0]),
                         H_pixel_to_court=cal.H)
    tr = CameraTracker(anchor)
    n = tr.set_anchor_image(frame)
    return tr, {"anchor_time": time, "anchor_features": n}