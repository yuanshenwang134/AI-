"""判断一段视频**适不适合**做俯视战术图（套餐 B）—— 拿素材先过这一关。

为什么需要：战术图的成败**几乎完全由素材决定**，而不是算法。
前面所有实测都指向同一组硬性前提，任何一个不满足都会让结果变成"看起来像
战术图、其实全错"的垃圾。与其事后猜，不如先把素材量一遍。

五项指标（都有实测阈值，不是拍脑袋）
------------------------------------------------------------------
  1. 机位是否固定   < 3 px/s   （单应标定只对固定机位成立）
  2. 球员框高度     > 100 px   （否则球衣颜色/号码分不开 —— 广播里只有 40~70px）
  3. 球检测命中率   > 40%      （球太小就追不到，传球网络必然是空的）
  4. 球场线是否够清晰 + 能否自动标定   ratio >= 1.6
  5. 画面里能看到几条边   >= 3  （底线 + 两条边线，标定至少需要 4 个角点）

用法
------------------------------------------------------------------
    python scripts/check_video_fit.py --video data\\mygame.mp4
    python scripts/check_video_fit.py --video mygame.mp4 --ball-weights runs\\detect\\ball\\weights\\best.pt

输出一张表 + 一句结论（适合 / 勉强 / 不适合），并给出**怎么改**的建议。
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="判断视频是否适合做俯视战术图")
    ap.add_argument("--video", required=True)
    ap.add_argument("--samples", type=int, default=24, help="抽样帧数")
    ap.add_argument("--imgsz", type=int, default=1280)
    ap.add_argument("--device", default="cpu")
    ap.add_argument("--ball-weights", default="",
                    help="球检测权重；给了才测第 3 项")
    a = ap.parse_args(argv)

    if not Path(a.video).exists():
        print(f"[err] 找不到视频：{a.video}")
        return 2

    import cv2
    import numpy as np
    from aihoop.calibcheck import camera_motion, auto_calibrate, frame_fit_detail

    cap = cv2.VideoCapture(a.video)
    if not cap.isOpened():
        print(f"[err] 打不开视频：{a.video}")
        return 2
    fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
    n = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
    W, H = int(cap.get(3)), int(cap.get(4))
    dur = n / fps if fps else 0
    print(f"视频: {W}x{H}  {fps:.1f}fps  {dur:.1f}s")
    step = max(1, n // max(1, a.samples))
    frames = []
    i = 0
    while len(frames) < a.samples:
        cap.set(cv2.CAP_PROP_POS_FRAMES, i)
        ok, f = cap.read()
        if not ok:
            break
        frames.append(f)
        i += step
    cap.release()

    rows = []

    # 1) 机位运动
    m = camera_motion(a.video)
    ok1 = m.get("verdict") in ("static",)
    rows.append(("机位是否固定", f"{m.get('median_px')} px/s (峰值 {m.get('max_px')})",
                 "< 3 px/s", ok1))

    # 2) 球员框高度
    med_h = 0.0
    try:
        from ultralytics import YOLO
        det = YOLO("yolov8n.pt")
        hs = []
        for f in frames[:12]:
            r = det.predict(f, conf=0.4, imgsz=a.imgsz, device=a.device,
                            verbose=False)[0]
            if r.boxes is None:
                continue
            for b in r.boxes:
                if str(r.names[int(b.cls[0])]) != "person":
                    continue
                x1, y1, x2, y2 = [float(v) for v in b.xyxy[0]]
                if (y2 - y1) < 25 or (y2 - y1) < (x2 - x1) * 0.9:
                    continue
                hs.append(y2 - y1)
        med_h = float(np.median(hs)) if hs else 0.0
    except Exception as e:  # noqa: BLE001
        print(f"[warn] 球员检测失败: {e}")
    ok2 = med_h > 100
    rows.append(("球员框高度", f"{med_h:.0f} px", "> 100 px", ok2))

    # Shared ball-detection verdict, independent of tactical geometry fitness.
    from aihoop.footage import probe_detector
    ball_fit = probe_detector(a.video,a.ball_weights,a.samples,a.imgsz,a.device)
    print("球检测体检：" + ball_fit["note"])
    rate = ball_fit["ball_detection_rate"]
    rows.append(("球检测覆盖率", f"{rate:.0%}" if rate is not None else "未测",
                 ">=40%（非标注召回率）", ball_fit["ok"]))

    # 4) 能否自动标定
    ratio = 0.0
    r = auto_calibrate(frames[len(frames) // 2], hints=None)
    ratio = float(r.get("ratio", 0) or 0)
    ok4 = ratio >= 1.6
    rows.append(("自动标定线拟合", f"ratio {ratio:.2f}", ">= 1.6", ok4))

    # 5) 场地可见性（用自动标定出的四边形判断它是否落在画面内且不退化）
    ok5 = bool(r.get("ok")) and ratio >= 1.6
    rows.append(("画面能否看全球场", "是" if ok5 else "否/看不清",
                 "底线+两条边线", ok5))

    print("\n%-15s %-28s %-12s %s" % ("指标", "实测", "要求", "结论"))
    print("-" * 72)
    for name, val, req, ok in rows:
        flag = "✅" if ok else ("—" if ok is None else "❌")
        print("%-15s %-28s %-12s %s" % (name, val, req, flag))

    hard = [r for r in rows if r[3] is False]
    print("\n战术坐标适用性（独立于上面的球检测体检）：", end="")
    if not hard:
        print("✅ 适合 —— 可以直接跑战术图（先 calibrate --interactive 标一次）")
    elif len(hard) <= 1:
        print("⚠️ 勉强 —— 能跑，但 %s 不达标，结果会打折扣"
              % "、".join(h[0] for h in hard))
    else:
        print("❌ 不适合 —— %s 都不达标。建议换素材："
              % "、".join(h[0] for h in hard))
        print("     机位固定（三脚架/看台）、俯角大（能看全半场）、"
              "离场近（球员占画面 1/4 以上）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())