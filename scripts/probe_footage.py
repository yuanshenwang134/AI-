<<<<<<< HEAD
"""素材体检使用真实检测器覆盖率。圆形色块仅是辅助线索，不能据此否决广角素材。"""
=======
"""素材体检：这段视频够不够"自动识别进球"？

为什么需要它：实测结论是"球在画面里太小/太糊时，任何算法都到不了随机以上"。
与其拿到素材先花几小时跑管线，不如先花 30 秒量一下**球有多大**。

判断依据（都是实测得出的门槛）：
  * 球的像素直径 ≥ 30px          → 检测器/轨迹判据可用
  * 球径 / 画面宽 ≥ 1/30         → 等价说法
  * 帧间位移 ≤ 1 个球径          → 否则球被拖成模糊条，形状特征丢失
  * 篮筐宽度 ≥ 300px             → 筐区细节够判"穿筐"

怎么量球：在整片里抽样若干帧，找"圆形 + 接近球场橙/黄蓝配色 + 尺寸合理"的块，
取中位数当球径（量不到就说明连球都找不到，那更不可能自动）。

用法：
    python scripts/probe_footage.py --video data/bili_nybo.mp4
    python scripts/probe_footage.py --video data/new_video.mp4 --hoop 361,135,19,7
"""
>>>>>>> a85d267883743e2b264c8702f6b94fcd5f78480f
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


<<<<<<< HEAD
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
=======
def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="素材体检：够不够自动识别")
    ap.add_argument("--video", required=True)
    ap.add_argument("--hoop", default=None, help="篮筐 cx,cy,rx,ry（可选，用于算筐宽）")
    ap.add_argument("--samples", type=int, default=240)
    ap.add_argument("--max-seconds", type=float, default=0.0)
    a = ap.parse_args(argv)

    cap = cv2.VideoCapture(a.video)
    if not cap.isOpened():
        print(f"打不开视频：{a.video}")
        return 2
    fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
    W = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    H = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
    dur = total / fps if fps else 0.0
    if a.max_seconds:
        dur = min(dur, a.max_seconds)
    step = max(1, int(dur * fps / max(1, a.samples)))

    sizes, centers = [], []
    f = 0
    while f < total and (f / fps) <= dur:
        cap.set(cv2.CAP_PROP_POS_FRAMES, f)
        ok, fr = cap.read()
        if not ok:
            break
        for d, x, y in round_blobs(fr):
            sizes.append(d)
            centers.append((f / fps, x, y, d))
        f += step
    cap.release()

    hoop_w = None
    if a.hoop:
        try:
            hoop_w = 2.0 * float(a.hoop.split(",")[2])
        except Exception:
            hoop_w = None

    print(f"素材 {Path(a.video).name}：{W}x{H} @ {fps:.1f}fps  时长 {dur:.0f}s")
    print(f"抽样 {a.samples} 帧，检出「像球」的圆块 {len(sizes)} 个")
    verdict = {}
    if not sizes:
        print("✗ 连「像球」的圆块都找不到 → 不可能自动识别。")
        verdict = {"ball_px": None, "ok": False}
    else:
        arr = np.array(sizes)
        med = float(np.median(arr))
        print(f"  球径估计：中位 {med:.1f}px  范围 {arr.min():.0f}~{arr.max():.0f}px")
        # 帧间位移（同一物体近似：用相邻采样点里最近的配对估算运动强度）
        frac = med / W
        print(f"  球径/画面宽 = 1/{W / max(1e-6, med):.0f}（门槛 1/30）")
        # 帧间位移：用相邻采样帧里"球块"的平均最近邻距离做粗估
        by_frame = {}
        for t, x, y, d in centers:
            by_frame.setdefault(round(t, 2), []).append((x, y))
        shifts = []
        ts = sorted(by_frame)
        for t0, t1 in zip(ts, ts[1:]):
            if t1 - t0 > 2.0:
                continue
            best = None
            for (x0, y0) in by_frame[t0]:
                for (x1, y1) in by_frame[t1]:
                    d = float(np.hypot(x1 - x0, y1 - y0))
                    if best is None or d < best:
                        best = d
            if best is not None and best < 300:
                shifts.append(best / max(1e-6, t1 - t0) / fps)   # px/帧
        shift_px = float(np.median(shifts)) if shifts else None
        checks = {
            "球径 ≥ 30px": med >= 30,
            "球径/画面宽 ≥ 1/30": frac >= 1 / 30.0,
        }
        if shift_px is not None:
            checks["帧间位移 ≤ 1 个球径"] = shift_px <= med
            print(f"  球帧间位移 ≈ {shift_px:.1f}px/帧（{'(模糊条)' if shift_px > med else '可以'}）")
        if hoop_w:
            checks["篮筐宽 ≥ 300px"] = hoop_w >= 300
            print(f"  篮筐宽 ≈ {hoop_w:.0f}px（门槛 300）")
        for k, v in checks.items():
            print(("  ✓ " if v else "  ✗ ") + k)
        ok = all(checks.values())
        print(f"\n体检结论：{'✅ 够用，可以做全自动' if ok else '❌ 不够用 —— 球太小/太糊，任何算法都到不了随机以上'}")
        if not ok:
            print("   建议：手机架到篮架/篮板支架上朝下拍；或长焦跟球；"
                  "并开 60fps（减少运动模糊）")
        verdict = {"ball_px": med, "ball_frac": round(frac, 5),
                   "shift_px": shift_px, "checks": checks, "ok": ok}
    (ROOT / "out" / "footage_probe.json").write_text(
        json.dumps({"video": a.video, "W": W, "H": H, "fps": fps, **verdict},
                   ensure_ascii=False, indent=2), encoding="utf-8")
    print("明细已存 out/footage_probe.json")
    return 0 if verdict.get("ok") else 1

>>>>>>> a85d267883743e2b264c8702f6b94fcd5f78480f

if __name__ == "__main__":
    raise SystemExit(main())
