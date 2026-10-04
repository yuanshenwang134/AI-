"""极简检测封装：YOLO 推理 + ByteTrack 跟踪。

设计目标：单一职责，不依赖 ultralytics 之外的库；每帧返回统一结构的检测结果。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable

import numpy as np


@dataclass
class Det:
    cls_name: str
    conf: float
    xyxy: tuple[float, float, float, float]
    track_id: int | None = None
    predicted: bool = False   # True = 由运动模型外推得到，非真实观测（不得用于确认事件）

    @property
    def cx(self) -> float:
        return (self.xyxy[0] + self.xyxy[2]) / 2.0

    @property
    def cy(self) -> float:
        return (self.xyxy[1] + self.xyxy[3]) / 2.0

    @property
    def w(self) -> float:
        return self.xyxy[2] - self.xyxy[0]

    @property
    def h(self) -> float:
        return self.xyxy[3] - self.xyxy[1]

    @property
    def area(self) -> float:
        return max(0.0, self.w) * max(0.0, self.h)

    def as_dict(self) -> dict:
        return {
            "cls": self.cls_name,
            "conf": round(float(self.conf), 3),
            "xyxy": [round(float(v), 1) for v in self.xyxy],
            "track_id": self.track_id,
        }


class YoloDetector:
    """YOLO 检测/跟踪封装。

    用法::

        det = YoloDetector("models/rim_ball.pt", conf=0.45)
        dets = det.detect(frame)                 # 纯检测
        dets = det.detect(frame, track=True)     # ByteTrack 跟踪(带 track_id)
    """

    def __init__(
        self,
        weights: str,
        conf: float = 0.4,
        iou: float = 0.5,
        imgsz: int = 640,
        device: str | None = None,
        class_names: Iterable[str] | None = None,
        half: bool = True,
    ) -> None:
        from ultralytics import YOLO  # 延迟导入，便于无依赖时给出清晰报错

        self.model = YOLO(weights)
        self.conf = conf
        self.iou = iou
        self.imgsz = imgsz
        self.device = device
        self.half = half
        # 允许按类别名过滤（例如只保留 person）
        self.class_names = set(class_names) if class_names else None
        self.names: dict[int, str] = {int(k): str(v) for k, v in self.model.names.items()}

    # ------------------------------------------------------------------ utils
    def _to_dets(self, result) -> list[Det]:
        boxes = getattr(result, "boxes", None)
        if boxes is None or len(boxes) == 0:
            return []
        xyxy = boxes.xyxy.detach().cpu().numpy()
        conf = boxes.conf.detach().cpu().numpy()
        cls = boxes.cls.detach().cpu().numpy().astype(int)
        ids = None
        if getattr(boxes, "id", None) is not None:
            ids = boxes.id.detach().cpu().numpy().astype(int)

        out: list[Det] = []
        for i in range(len(xyxy)):
            name = self.names.get(int(cls[i]), str(cls[i]))
            if self.class_names and name not in self.class_names:
                continue
            out.append(
                Det(
                    cls_name=name,
                    conf=float(conf[i]),
                    xyxy=tuple(float(v) for v in xyxy[i]),
                    track_id=int(ids[i]) if ids is not None else None,
                )
            )
        return out

    # ------------------------------------------------------------------- api
    def detect(self, frame: np.ndarray, track: bool = False) -> list[Det]:
        # 注意：不再传 half=，ultralytics 8.4 已弃用该参数并会每帧打印警告；
        # 默认精度下本机速度足够（27 秒视频约 45 秒跑完）。
        kwargs = dict(
            conf=self.conf,
            iou=self.iou,
            imgsz=self.imgsz,
            verbose=False,
        )
        if self.device:
            kwargs["device"] = self.device
        if track:
            results = self.model.track(
                frame, persist=True, tracker="bytetrack.yaml", **kwargs
            )
        else:
            results = self.model.predict(frame, **kwargs)
        return self._to_dets(results[0])

    def warmup(self) -> None:
        try:
            dummy = np.zeros((self.imgsz, self.imgsz, 3), dtype=np.uint8)
            self.detect(dummy)
        except Exception:  # 预热失败不影响主流程
            pass


def pick_center(objs: list[Det], prefer_cls: str | None = None, min_conf: float = 0.0) -> Det | None:
    """从候选里挑一个（默认取面积最大、置信度最高者）。"""
    cands = [o for o in objs if (prefer_cls is None or o.cls_name == prefer_cls) and o.conf >= min_conf]
    if not cands:
        return None
    return max(cands, key=lambda o: (o.area * 0.5 + o.conf * 100.0))


def nearest_person(persons: list[Det], x: float, y: float) -> Det | None:
    """返回距离 (x, y) 最近的球员检测。"""
    if not persons:
        return None
    return min(persons, key=lambda p: (p.cx - x) ** 2 + (p.cy - y) ** 2)


def filter_by_size(dets: list[Det], frame_width: float,
                   min_ratio: float = 0.012, max_ratio: float = 0.16) -> list[Det]:
    """按"框宽占画面比例"过滤：篮球不可能占据画面很大比例。

    这一步专治检测器在域外场景的"大框误报"（例如把一片场地/背景判成 basketball）。
    """
    lo, hi = min_ratio * frame_width, max_ratio * frame_width
    return [d for d in dets if lo <= d.w <= hi]


def pick_best(cands: list[Det], last_xy: tuple[float, float] | None = None,
              dist_weight: float = 0.002) -> Det | None:
    """从候选中选一个：有历史位置时优先"置信度 - 距离惩罚"（简单数据关联），否则取置信度最高。"""
    if not cands:
        return None
    if last_xy is None:
        return max(cands, key=lambda d: d.conf)

    def score(d: Det) -> float:
        dist = ((d.cx - last_xy[0]) ** 2 + (d.cy - last_xy[1]) ** 2) ** 0.5
        return d.conf - dist_weight * dist

    return max(cands, key=score)


def pick_rim(cands: list[Det], min_conf: float = 0.35, min_aspect: float = 1.2,
             allow_shape_fallback: bool = False) -> tuple[Det | None, dict[str, int]]:
    """挑篮筐框：必须"高置信度 + 宽扁形状"（侧面视角的筐是宽的，不是方的）。

    返回 (选中的框, 统计)。默认**不做形状回退**——宁可这一帧没有筐，也不接受方框候选，
    否则错误候选会被计分引擎当成篮筐（源码审查报告 P1-3）。
    统计里 rejected_shape / rejected_conf 用于观测"被拒了多少候选"。
    """
    stats = {"raw": len(cands), "rejected_conf": 0, "rejected_shape": 0, "accepted": 0}
    strong = []
    for d in cands:
        if d.conf < min_conf:
            stats["rejected_conf"] += 1
        else:
            strong.append(d)
    wide = [d for d in strong if d.h <= 0 or (d.w / max(d.h, 1e-6)) >= min_aspect]
    stats["rejected_shape"] = len(strong) - len(wide)
    pool = wide or (strong if allow_shape_fallback else [])
    if not pool:
        return None, stats
    stats["accepted"] = 1
    return max(pool, key=lambda d: d.conf), stats


