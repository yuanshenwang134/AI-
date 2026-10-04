"""素材体检使用真实检测器覆盖率。圆形色块仅是辅助线索，不能据此否决广角素材。"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import cv2                                                   # noqa: E402
import numpy as np                                           # noqa: E402


def round_blobs(frame, min_area=8, max_area=4000):
    """找"像球"的圆块：颜色（球场常见橙/黄蓝）+ 圆形度。返回 [(直径, x, y)]。"""
    hsv = cv2.cvtColor(frame, cv2.COLOR_BGR2HSV)
    masks = [
        cv2.inRange(hsv, (5, 90, 80), (30, 255, 255)),      # 橙色球
        cv2.inRange(hsv, (18, 90, 90), (40, 255, 255)),     # 黄
        cv2.inRange(hsv, (85, 60, 60), (135, 255, 255)),    # 蓝
    ]
    out = []
    for m in masks:
        m = cv2.morphologyEx(m, cv2.MORPH_OPEN, np.ones((3, 3), np.uint8))
        n, lab, stats, cent = cv2.connectedComponentsWithStats(m, 8)
        for i in range(1, n):
            x, y, w, h, a = (int(stats[i, 0]), int(stats[i, 1]),
                             int(stats[i, 2]), int(stats[i, 3]),
                             int(stats[i, 4]))
            if a < min_area or a > max_area:
                continue
            ar = w / max(1.0, h)
            if not (0.6 <= ar <= 1.7):
                continue
            if a / max(1.0, w * h) < 0.6:          # 填充率：圆块
                continue
            out.append(((w + h) / 2.0, x + w / 2, y + h / 2))
    return out


def main(argv=None):
    from aihoop.footage import probe_detector
    ap=argparse.ArgumentParser(description="素材体检：共享检测器覆盖率，不以色块大小否决素材")
    ap.add_argument("--video",required=True)
    ap.add_argument("--ball-weights",default="")
    ap.add_argument("--samples",type=int,default=24)
    ap.add_argument("--imgsz",type=int,default=1280)
    ap.add_argument("--device",default="cpu")
    ap.add_argument("--max-seconds",type=float,default=0.)
    ap.add_argument("--hoop",default=None,help="兼容旧参数；不作为素材否决条件")
    a=ap.parse_args(argv)
    verdict=probe_detector(a.video,a.ball_weights,a.samples,a.imgsz,a.device,a.max_seconds)
    verdict["video"]=a.video
    (ROOT/"out").mkdir(parents=True,exist_ok=True)
    (ROOT/"out/footage_probe.json").write_text(json.dumps(verdict,ensure_ascii=False,indent=2),encoding="utf-8")
    print(verdict["note"])
    print(f"检测到球的采样帧：{verdict['hit_frames']}/{verdict['sampled_frames']}；这不是标注召回率或投篮准确率。")
    return 0 if verdict["ok"] else (2 if verdict["ok"] is None else 1)

if __name__ == "__main__":
    raise SystemExit(main())
