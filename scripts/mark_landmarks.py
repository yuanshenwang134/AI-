"""在画面上标点：告诉分析管线「篮筐在哪、圈多大、攻哪边」。

为什么需要它（这是被真实误判逼出来的）：
  * 这个机位上我的篮筐检测器命中率不稳、判据阈值也没有真实尺度，
    结果把「球贴着篮圈外沿斜掠而下」判成了进球（用户指出 59.94s / 127.67s）。
  * 你花 30 秒点一次，就能把「篮筐中心 + 圈边界」变成已知量：
    判据里的 rx/ry 有了真值，圈内/圈外的边界不再是猜的；
    检测器认不出来也无所谓 —— 直接用你标的坐标。

用法
------------------------------------------------------------------
    # 默认在第 1 秒的帧上标（可以先翻帧找一个篮筐清楚的时刻）
    python scripts\\mark_landmarks.py --video data\\nybo_3min.mp4

    # 指定用第几秒的帧（篮球在空中/被挡的时候不适合标，换一帧）
    python scripts\\mark_landmarks.py --video data\\nybo_3min.mp4 --at 12.5

    # 只想标篮筐中心（偷懒也行，半径会用保守默认值）
    python scripts\\mark_landmarks.py --video data\\nybo_3min.mp4 --need rim_center

    # 无界面：直接给坐标（cx,cy 必填；--rim-box 给 x0,y0,x1,y1）
    python scripts\\mark_landmarks.py --video data\\nybo_3min.mp4 `
        --rim 72,58 --rim-box 14,34,130,82 --out data\\marks_nybo.json

产出
------------------------------------------------------------------
    data/marks_<视频名>.json
      { "landmarks": {...}, "hoop": [cx, cy, rx, ry], "video": "..." }
    之后的视频分析可以直接吃它（见 aihoop.cli video --marks）。
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from basket_label_lib import (load_marks, pick_landmarks,  # noqa: E402
                              require_cv, save_marks, landmarks_to_hoop,
                              LANDMARKS)

KEYS = [k for k, _n, _h in LANDMARKS]


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="在画面上标出篮筐位置（供分析使用）")
    ap.add_argument("--video", required=True)
    ap.add_argument("--at", type=float, default=1.0, help="用第几秒的帧来标")
    ap.add_argument("--out", default=None, help="输出 json（默认 data/marks_<视频名>.json）")
    ap.add_argument("--zoom", type=float, default=1.0, help="显示放大倍数（远处篮筐建议 2）")
    ap.add_argument("--need", default=None,
                    help="只标这几项，逗号分隔：rim_center,rim_left,rim_right,rim_top,rim_bottom")
    ap.add_argument("--rim", default=None, help="无界面：篮筐中心 x,y")
    ap.add_argument("--rim-box", default=None,
                    help="无界面：篮圈外接框 x0,y0,x1,y1（用来算 rx/ry）")
    ap.add_argument("--show", action="store_true", help="只把 --at 的帧存成图看看")
    args = ap.parse_args(argv)

    cv2, _np = require_cv()
    video = str(args.video)
    out_path = args.out or str(ROOT / "data" /
                               f"marks_{Path(video).stem}.json")

    cap = cv2.VideoCapture(video)
    if not cap.isOpened():
        print(f"[err] 打不开视频：{video}")
        return 2
    fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
    cap.set(cv2.CAP_PROP_POS_FRAMES, int(args.at * fps))
    ok, frame = cap.read()
    cap.release()
    if not ok:
        print(f"[err] 取不到第 {args.at}s 的帧")
        return 2

    # 无界面模式：直接写坐标
    if args.rim or args.show:
        if args.show and not args.rim:
            p = ROOT / "out" / f"mark_frame_{args.at:.1f}s.png"
            p.parent.mkdir(parents=True, exist_ok=True)
            cv2.imwrite(str(p), frame)
            print(f"已存帧：{p}（用任意看图工具读坐标，再 --rim x,y）")
            return 0
        try:
            cx, cy = [float(v) for v in str(args.rim).split(",")]
        except Exception:
            print("[err] --rim 格式应为 cx,cy")
            return 2
        marks = {"rim_center": (cx, cy)}
        if args.rim_box:
            try:
                x0, y0, x1, y1 = [float(v) for v in str(args.rim_box).split(",")]
            except Exception:
                print("[err] --rim-box 格式应为 x0,y0,x1,y1")
                return 2
            marks.update({"rim_left": (x0, (y0 + y1) / 2),
                          "rim_right": (x1, (y0 + y1) / 2),
                          "rim_top": ((x0 + x1) / 2, y0),
                          "rim_bottom": ((x0 + x1) / 2, y1)})
        hoop = landmarks_to_hoop(marks)
        save_marks(out_path, video, marks, hoop,
                   extra={"at": args.at, "mode": "cli"})
        print(f"[ok] 已保存：{out_path}")
        print(f"     篮筐 cx,cy,rx,ry = {hoop.cx:.0f},{hoop.cy:.0f},"
              f"{hoop.rx:.0f},{hoop.ry:.0f}")
        return 0

    # 交互标点
    need = [s.strip() for s in args.need.split(",")] if args.need else None
    if need:
        bad = [n for n in need if n not in KEYS]
        if bad:
            print(f"[err] --need 里有不认识的点：{bad}；可用：{KEYS}")
            return 2
    print("在窗口里点：左键=落点，右键=撤销，N=跳过当前，S=保存，Q=放弃")
    print("（远处篮筐看不清时，重开一次并加 --zoom 2 放大）")
    marks = pick_landmarks(frame, zoom=args.zoom, need=need)
    if not marks:
        print("已取消（没有保存）")
        return 1
    hoop = landmarks_to_hoop(marks)
    if hoop is None:
        print("[warn] 没有点到篮筐中心（rim_center），未保存")
        return 1
    save_marks(out_path, video, marks, hoop,
               extra={"at": args.at, "mode": "interactive",
                      "frame_size": [frame.shape[1], frame.shape[0]]})
    print(f"[ok] 已保存：{out_path}")
    print(f"     篮筐 cx,cy,rx,ry = {hoop.cx:.0f},{hoop.cy:.0f},"
          f"{hoop.rx:.0f},{hoop.ry:.0f}")
    if hoop.rx <= 0 or hoop.ry <= 0:
        print("[warn] 半径仍为默认值：建议再标一次左右缘/上下沿")
    print("\n下一步（用这份标点跑分析）：")
    print(f"  $env:PYTHONPATH='src'; python -m aihoop.cli video "
          f"--video {video} --marks {out_path} --cal data\\calibration.json "
          f"--out out\\nybo_marked --device 0")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
