"""在你的视频上点球场关键点 → 生成微调数据 → 让自动标定在你的机位可用。

为什么需要这一步（实测）：
  公开数据集训的模型在你机位上**认得出 14 个点，但位置系统性错位** ——
  自动标定出来的结果自己觉得误差只有 1.59m，可你亲手标的真篮筐却被投到
  球场外 23m。这种"内部自洽、物理错误"的结果最危险，所以必须用**你机位的真值**微调。

14 个关键点（顺序与数据集一致，编号不能乱）：
   0 远端底线·左角      1 远端底线·中点      2 远端底线·右角
   3 右边线·远端半场    4 右边线·近端半场
   5 近端底线·右角      6 近端底线·中点      7 近端底线·左角
   8 左边线·近端半场    9 左边线·远端半场
  10/11 内侧·左（远/近） 12/13 内侧·右（远/近）
  ※ 10~13 在球场上**没有对应画线**（是数据集作者放的参考点），
    你在自己视频里多半找不到 —— **按 N 跳过即可**，模型训练允许缺关键点。

用法：
    # 1) 抽帧并逐个标注（弹窗点击；和 label_ball.py 一样的操作方式）
    python scripts\\mark_court_kp.py --video data\\new_video.mp4 `
        --at 60,120,180 --out data/calib_ds_finetune

    # 2) 训练（在基础模型上微调）
    python scripts\\train_court_keypoints.py --ds data/calib_ds_finetune `
        --finetune runs/pose/court_kp/weights/best.pt --epochs 40

键鼠：
    左键   点在当前提示的关键点上（点了自动跳下一个）
    N      这个点在画面里看不到 → 跳过（标为不可见）
    B      回上一个点
    S      保存这一帧并进入下一帧
    Q      放弃退出
"""
from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# 编号 → (中文提示, 说明)。提示语按"机位在近端朝远端看"的常见转播视角写。
KP_HINTS = {
    0: ("远端底线·左角", "画面深处那条底线，与左边线的交点"),
    1: ("远端底线·中点", "画面深处那条底线的正中（篮筐正下方那条线的中点）"),
    2: ("远端底线·右角", "画面深处那条底线，与右边线的交点"),
    3: ("右边线·远端半场", "右边线上，靠远处那一半的中间位置"),
    4: ("右边线·近端半场", "右边线上，靠近你这一半的中间位置"),
    5: ("近端底线·右角", "画面近处那条底线的右角（可能在画面外）"),
    6: ("近端底线·中点", "画面近处那条底线的正中"),
    7: ("近端底线·左角", "画面近处那条底线的左角"),
    8: ("左边线·近端半场", "左边线上，靠近你这一半的中间位置"),
    9: ("左边线·远端半场", "左边线上，靠远处那一半的中间位置"),
    10: ("内侧·左（远端）", "球场内部偏左、偏远处（没有画线，多半看不到 → 按 N）"),
    11: ("内侧·左（近端）", "球场内部偏左、偏近处（多半看不到 → 按 N）"),
    12: ("内侧·右（远端）", "球场内部偏右、偏远处（多半看不到 → 按 N）"),
    13: ("内侧·右（近端）", "球场内部偏右、偏近处（多半看不到 → 按 N）"),
}
KPT = 14


def main(argv=None) -> int:
    import cv2
    import numpy as np

    ap = argparse.ArgumentParser(description="在你视频上点球场关键点（生成微调数据）")
    ap.add_argument("--video", required=True)
    ap.add_argument("--at", required=True,
                    help="用哪几秒的帧来标，逗号分隔（挑场地看得最全的，如 60,120,180）")
    ap.add_argument("--out", default="data/calib_ds_finetune")
    ap.add_argument("--base-ds", default="data/calib_ds",
                    help="基础数据集（微调时和你的标注合并）")
    a = ap.parse_args(argv)

    times = [float(x) for x in a.at.split(",") if x.strip()]
    out = ROOT / a.out
    (out / "train" / "images").mkdir(parents=True, exist_ok=True)
    (out / "train" / "labels").mkdir(parents=True, exist_ok=True)

    cap = cv2.VideoCapture(str(a.video))
    if not cap.isOpened():
        print(f"[err] 打不开视频：{a.video}")
        return 2
    fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
    W = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    H = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    print(f"视频 {W}x{H} @ {fps:.1f}fps；要标 {len(times)} 帧")
    print("左键=点当前提示的点  N=看不到/跳过  B=上一个  S=存这一帧  Q=退出")

    cv2.namedWindow("mark_court_kp", cv2.WINDOW_NORMAL)
    cv2.resizeWindow("mark_court_kp", 1100, 760)
    ctx = {"mouse": (0, 0), "click": None}

    def on_event(event, x, y, flags, param):
        if event == cv2.EVENT_MOUSEMOVE:
            param["mouse"] = (x, y)
        elif event == cv2.EVENT_LBUTTONDOWN:
            param["click"] = (x, y)

    cv2.setMouseCallback("mark_court_kp", on_event, ctx)

    n_done = 0
    skipped = set()          # 每帧开始时重置（必须在这里初始化，否则第一帧就 NameError）
    for ti, t in enumerate(times):
        skipped = set()      # 本帧跳过的点集合
        cap.set(cv2.CAP_PROP_POS_FRAMES, max(0, int(t * fps)))
        ok, frame = cap.read()
        if not ok:
            print(f"  第 {t}s 取帧失败，跳过")
            continue
        pts = {}          # idx -> (x, y) 像素
        idx = 0
        while idx < KPT:
            show = frame.copy()
            # 已点的
            for i, (x, y) in pts.items():
                cv2.circle(show, (int(x), int(y)), 6, (0, 255, 0), -1)
                cv2.putText(show, str(i), (int(x) + 8, int(y) - 8),
                            cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0, 255, 0), 2)
            # 已跳过（标为不可见）的用红线划掉
            for i in skipped:
                cv2.putText(show, f"{i} 跳过", (10, 60 + 22 * len([s for s in skipped if s <= i])),
                            cv2.FONT_HERSHEY_SIMPLEX, 0.55, (0, 0, 255), 2)
            name, hint = KP_HINTS[idx]
            cv2.rectangle(show, (0, 0), (W, 56), (0, 0, 0), -1)
            cv2.putText(show, f"[{ti + 1}/{len(times)}] t={t:.1f}s   "
                              f"现在点：kp{idx} {name}",
                        (8, 22), cv2.FONT_HERSHEY_SIMPLEX, 0.62,
                        (255, 255, 255), 2)
            cv2.putText(show, f"{hint}    （左键点 / N 看不到 / B 上一个 / S 存 / Q 退出）",
                        (8, 46), cv2.FONT_HERSHEY_SIMPLEX, 0.55,
                        (0, 255, 255), 1)
            mx, my = ctx["mouse"]
            cv2.drawMarker(show, (mx, my), (0, 165, 255), cv2.MARKER_CROSS, 22, 2)
            cv2.imshow("mark_court_kp", show)
            if ctx["click"] is not None:
                x, y = ctx["click"]
                ctx["click"] = None
                if y > 56:                     # 别把顶栏点了
                    pts[idx] = (float(x), float(y))
                    idx += 1
                    continue
            k = cv2.waitKey(30) & 0xFF
            if k == 255:
                continue
            if k in (ord('n'), ord('N')):
                skipped.add(idx)
                idx += 1
            elif k in (ord('b'), ord('B')):
                if idx > 0:
                    idx -= 1
                    pts.pop(idx, None)
                    skipped.discard(idx)
            elif k in (ord('s'), ord('S')):
                break
            elif k in (ord('q'), ord('Q')):
                cv2.destroyAllWindows()
                print("已放弃")
                return 1

        # 写 YOLO-pose 标签：class cx cy w h  (x y v)×14
        stem = f"{Path(a.video).stem}_t{int(t * 1000):08d}"
        cv2.imwrite(str(out / "train" / "images" / f"{stem}.jpg"), frame)
        xs = [p[0] / W for p in pts.values()]
        ys = [p[1] / H for p in pts.values()]
        if pts:
            cx = (min(xs) + max(xs)) / 2
            cy = (min(ys) + max(ys)) / 2
            bw = max(0.05, max(xs) - min(xs))
            bh = max(0.05, max(ys) - min(ys))
        else:
            cx = cy = 0.5; bw = bh = 1.0
        parts = ["0", f"{cx:.6f}", f"{cy:.6f}", f"{bw:.6f}", f"{bh:.6f}"]
        for i in range(KPT):
            if i in pts:
                parts += [f"{pts[i][0] / W:.6f}", f"{pts[i][1] / H:.6f}", "2"]
            else:
                parts += ["0", "0", "0"]
        (out / "train" / "labels" / f"{stem}.txt").write_text(
            " ".join(parts) + "\n", encoding="utf-8")
        n_done += 1
        print(f"  已存第 {ti + 1} 帧：标了 {len(pts)} 个点，跳过 {len(skipped)} 个")
    cap.release()
    cv2.destroyAllWindows()

    # 组织成可直接训练的数据集（合并基础数据集 + 你的标注）
    base = ROOT / a.base_ds
    if base.exists():
        for split in ("valid", "test"):
            for sub in ("images", "labels"):
                dst = out / split / sub
                dst.mkdir(parents=True, exist_ok=True)
                if (base / split / sub).exists():
                    for f in (base / split / sub).iterdir():
                        if f.is_file() and not (dst / f.name).exists():
                            shutil.copy(f, dst / f.name)
        for f in (base / "train" / "images").iterdir():
            if f.is_file() and not (out / "train" / "images" / f.name).exists():
                shutil.copy(f, out / "train" / "images" / f.name)
        for f in (base / "train" / "labels").iterdir():
            if f.is_file() and not (out / "train" / "labels" / f.name).exists():
                shutil.copy(f, out / "train" / "labels" / f.name)
        print("  已把基础数据集的图与标注复制进来（合并训练）")
    (out / "data.yaml").write_text(
        f"train: ../train/images\nval: ../valid/images\ntest: ../test/images\n"
        f"kpt_shape: [{KPT}, 3]\n"
        f"flip_idx: [{', '.join(str(i) for i in range(KPT))}]\n"
        f"nc: 1\nnames: ['basketball_court']\n", encoding="utf-8")

    print(f"\n完成：标了 {n_done} 帧 → {out}")
    print("下一步（在你自己的终端跑，你 GPU 上只要 1~2 分钟）：")
    print(f"  .venv\\Scripts\\python.exe scripts\\train_court_keypoints.py "
          f"--ds {a.out} --finetune runs/pose/court_kp/weights/best.pt --epochs 40")
    print("然后把 --weights 指到新权重，重跑 auto_calibrate.py，"
          "并用人工标的篮筐做独立校验（应该落回 ±1.5m 内）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
