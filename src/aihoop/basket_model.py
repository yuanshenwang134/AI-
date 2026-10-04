"""进球判定的**学习式**打分器：加载训练好的小 CNN，给候选进球打分。

为什么需要它：手写判据在 `nybo_3min` 上 precision = 0%（3 个判定全错），
调阈值救不了。训练出来的模型（scripts/train_basket_model.py）在
留一法上 AUC 0.89，并且在"真值全为没进"的 nybo 上**一个都不误报** ——
所以把它的分数接进管线，作为判定的置信度来源。

用法（管线侧）：
    from aihoop.basket_model import BasketScorer
    sc = BasketScorer.load("out/basket_model")      # 没有模型时 load 返回 None
    sc.score_candidate(video, t, hoop, cfg)         # → 0~1

设计上的两个要点：
  1. **切片必须和训练时同源**：训练用的篮筐位置来自同一份球场标定反算，
     所以这里也走 `scripts/export_basket_patches.py` 的同一套函数
     （`hoop_px_from_cal` / `export_patch` / `net_features`），避免训练/推理不一致。
  2. **模型可缺省**：没有模型文件就返回 None，管线退回原判据 ——
     不让"没训练过"变成跑不起来的理由。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Optional

# 本文件在 <项目根>/src/aihoop/basket_model.py → parents[2] 才是项目根。
# （写错过一次：parents[1] 得到 src/，于是 sys.path 里进的是 src/scripts、src/src，
#   import 全失败 → score_candidate 静默返回 None，看起来像"模型不工作"。）
ROOT = Path(__file__).resolve().parents[2]
_SCRIPTS = ROOT / "scripts"
_SRC = ROOT / "src"
for _p in (str(_SCRIPTS), str(_SRC)):
    if _p not in sys.path:
        sys.path.insert(0, _p)


class BasketScorer:
    """进球/没进的二分类打分器（小 CNN + 可选逻辑回归对照）。"""

    def __init__(self, model_dir: Path, net=None, logreg: Optional[dict] = None):
        self.dir = Path(model_dir)
        self.net = net
        self.logreg = logreg
        rep = self.dir / "report.json"
        self.report = json.loads(rep.read_text(encoding="utf-8")) if rep.exists() else {}

    # ---- 载入 ----
    @classmethod
    def load(cls, model_dir="out/basket_model"):
        d = Path(model_dir)
        if not (d / "cnn.pt").exists():
            return None
        try:
            import torch
        except Exception:
            return None
        # 只依赖 cnn.pt + 这里复现的网络结构。
        # 不要 import 训练脚本 —— 它是 scripts/ 下的独立脚本，
        # 包里不一定能 import 到（之前就这样：load() 静默返回 None）。
        net = _build_net()
        try:
            net.load_state_dict(torch.load(str(d / "cnn.pt"), map_location="cpu"))
            net.eval()
        except Exception:
            return None
        lr = None
        lp = d / "logreg.json"
        if lp.exists():
            try:
                lr = json.loads(lp.read_text(encoding="utf-8"))
            except Exception:
                lr = None
        return cls(d, net=net, logreg=lr)

    # ---- 打分 ----
    def score_candidate(self, video: str, t: float, hoop_px, patch_px: int = 96,
                        gap_s: float = 0.12) -> Optional[float]:
        """给「t 时刻篮筐附近是不是一次进球」打分（0~1）。失败返回 None。"""
        if self.net is None:
            return None
        try:
            import numpy as np
            import torch
            from export_basket_patches import export_patch
            from basket_label_lib import require_cv
        except Exception:
            return None
        cv2, np = require_cv()
        cx, cy = float(hoop_px[0]), float(hoop_px[1])
        cap = cv2.VideoCapture(str(video))
        if not cap.isOpened():
            return None
        fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
        tmp = self.dir / "_tmp_patch.npy"
        arr, _xy, _fulls = export_patch(cv2, np, cap, fps, float(t), cx, cy, tmp)
        cap.release()
        if arr is None:
            return None
        try:
            x = torch.tensor(arr.astype(np.float32) / 255.0).unsqueeze(0)
            if x.ndim == 3:                 # (2,H,W) -> (1,2,H,W)
                x = x.unsqueeze(0)
            with torch.no_grad():
                s = torch.sigmoid(self.net(x)).item()
            return float(s)
        except Exception:
            return None
        finally:
            try:
                tmp.unlink()
            except OSError:
                pass

    def threshold(self) -> float:
        """建议阈值：训练负样本的 p95（≈5% 误报率），来自 report.json。"""
        t = (self.report.get("threshold") or {})
        return float(t.get("cnn", 0.5))

    def summary(self) -> dict:
        r = dict(self.report.get("loo_auc") or {})
        return {"model_dir": str(self.dir),
                "loo_auc_cnn": r.get("cnn"),
                "threshold": self.threshold(),
                "note": "留一法 AUC（正样本仅 8 个，规模小，别当生产级指标）"}


def _build_net():
    """与 scripts/train_basket_model.py 里结构一致的小 CNN（2×96×96 → logit）。"""
    import torch.nn as nn

    class Net(nn.Module):
        def __init__(self):
            super().__init__()
            self.f = nn.Sequential(
                nn.Conv2d(2, 12, 5, stride=2, padding=2), nn.ReLU(),
                nn.Conv2d(12, 24, 3, stride=2, padding=1), nn.ReLU(),
                nn.Conv2d(24, 32, 3, stride=2, padding=1), nn.ReLU(),
                nn.AdaptiveAvgPool2d(4), nn.Flatten(),
                nn.Linear(32 * 16, 32), nn.ReLU(), nn.Dropout(0.3),
                nn.Linear(32, 1))

        def forward(self, x):
            return self.f(x).squeeze(-1)

    return Net()
