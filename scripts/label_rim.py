"""篮筐检测的训练数据标注工具 —— **固定机位只需点一次**。

为什么这样做
------------------------------------------------------------------
篮筐是"几十像素的小目标 + 环形 + 颜色和球衣重叠"，手工特征根本靠不住
（实测三条路全败：颜色启发式覆盖率只有 1~3%、ShotTracker 的 rim 类把左上角
水印当篮筐、广播数据集训出来的 rim 类完全没学会）。

所以必须训练专用检测器 —— 但标注是瓶颈。这里的关键观察是：
**固定机位下篮筐在画面里根本不动**（实测机位漂移 0.06 px/s），
所以「点一次 → 给这段视频的所有抽样帧生成同一份标注」，
一次点击就能产出上百张训练图。三段素材 = 三次点击 ≈ 600 张。

用法
------------------------------------------------------------------
    # 弹窗点击（推荐）：鼠标左键点篮筐中心，滚轮/±调框大小，S 保存，Q 取消
    python scripts\\label_rim.py --video data\\nybo_3min.mp4

    # 无界面：已经从网格图读出坐标就手填 x,y,w,h
    python scripts\\label_rim.py --video data\\nybo_3min.mp4 --rim 1150,400,120,50

    # 多段视频攒成一个数据集（--out 用同一个）
    python scripts\\label_rim.py --video data\\basketball_game.mp4 --out data\\rim_ds

产出（YOLO 格式，单类 rim）：
    data/rim_ds/images/xxx.jpg     每 --stride 帧抽一张
    data/rim_ds/labels/xxx.txt     每行 "0 cx cy w h"（归一化）
    data/rim_ds/data.yaml
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _grid(img):
    """画 100px 网格 + 坐标刻度，方便读坐标。"""
    import cv2
    h, w = img.shape[:2]
    out = img.copy()
    for x in range(0, w, 100):
        c = (0, 255, 0) if x % 500 == 0 else (0, 160, 0)
        cv2.line(out, (x, 0), (x, h), c, 2 if x % 500 == 0 else 1)
        if x % 200 == 0:
            cv2.putText(out, str(x), (x + 4, 26), cv2.FONT_HERSHEY_SIMPLEX,
                        0.7, (0, 255, 255), 2)
    for y in range(0, h, 100):
        c = (0, 255, 0) if y % 500 == 0 else (0, 160, 0)
        cv2.line(out, (0, y), (w, y), c, 2 if y % 500 == 0 else 1)
        if y % 200 == 0:
            cv2.putText(out, str(y), (6, y + 26), cv2.FONT_HERSHEY_SIMPLEX,
                        0.7, (0, 255, 255), 2)
    return out


def _pick_by_click(frame):
    """弹窗点选篮筐：左键点中心，滚轮/± 调框，S 保存，Q 取消。返回 (cx,cy,w,h)。"""
    import cv2
    state = {"c": None, "w": 120, "h": 50}

    def on_mouse(ev, x, y, flags, _p):
        if ev == cv2.EVENT_LBUTTONDOWN:
            state["c"] = (x, y)
        elif ev == cv2.EVENT_MOUSEWHEEL:
            d = 10 if flags > 0 else -10
            state["w"] = max(20, state["w"] + d)
            state["h"] = max(10, state["h"] + d // 2)
        elif ev == cv2.EVENT_MOUSEMOVE and state["c"] and (flags & 1):
            state["c"] = (x, y)

    win = "click the RIM center  (mouse wheel = box size, s = save, q = quit)"
    cv2.namedWindow(win, cv2.WINDOW_NORMAL)
    cv2.setMouseCallback(win, on_mouse)
    while True:
        view = _grid(frame)
        cv2.putText(view, "click RIM center | wheel=size | s=save | q=quit",
                    (20, 60), cv2.FONT_HERSHEY_SIMPLEX, 1.1, (0, 255, 255), 3)
        if state["c"]:
            cx, cy = state["c"]
            x1, y1 = int(cx - state["w"] / 2), int(cy - state["h"] / 2)
            cv2.rectangle(view, (x1, y1), (x1 + state["w"], y1 + state["h"]),
                          (0, 0, 255), 3)
            cv2.drawMarker(view, (cx, cy), (0, 0, 255), cv2.MARKER_CROSS, 24, 2)
            cv2.putText(view, f"({cx},{cy}) {state['w']}x{state['h']}",
                        (20, 110), cv2.FONT_HERSHEY_SIMPLEX, 0.9, (0, 0, 255), 2)
        cv2.imshow(win, view)
        k = cv2.waitKey(30) & 0xFF
        if k in (ord("q"), 27):
            cv2.destroyAllWindows()
            return None
        if k == ord("s") and state["c"]:
            cv2.destroyAllWindows()
            return (state["c"][0], state["c"][1], state["w"], state["h"])


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="标注篮筐（固定机位只需点一次）")
    ap.add_argument("--video", required=True)
    ap.add_argument("--out", default="data/rim_ds")
    ap.add_argument("--stride", type=int, default=30, help="每多少帧抽一张训练图")
    ap.add_argument("--max-frames", type=int, default=150)
    ap.add_argument("--at", type=float, default=0.5, help="用第几成处的帧来点选")
    ap.add_argument("--rim", default="", help="不给就弹窗点击：x,y,w,h")
    ap.add_argument("--only-frames", action="store_true",
                    help="只抽图不写标注（用于补充负样本）")
    a = ap.parse_args(argv)

    if not Path(a.video).exists():
        print(f"[err] 找不到视频：{a.video}")
        return 2
    try:
        import cv2
    except ImportError:
        print("[err] 需要 opencv-python")
        return 2

    cap = cv2.VideoCapture(a.video)
    if not cap.isOpened():
        print(f"[err] 打不开视频：{a.video}")
        return 2
    W, H = int(cap.get(3)), int(cap.get(4))
    n = int(cap.get(7) or 0)
    cap.set(cv2.CAP_PROP_POS_FRAMES, int(n * a.at))
    ok, ref = cap.read()
    if not ok:
        print("[err] 读不到参考帧")
        return 2

    if a.rim:
        try:
            cx, cy, bw, bh = [float(v) for v in a.rim.split(",")]
        except ValueError:
            print("[err] --rim 格式应为 x,y,w,h")
            return 2
    elif a.only_frames:
        cx = cy = bw = bh = 0
    else:
        picked = _pick_by_click(ref)
        if not picked:
            print("[cancel] 已取消")
            return 1
        cx, cy, bw, bh = picked
    print(f"篮筐: 中心({cx:.0f},{cy:.0f}) 框 {bw:.0f}x{bh:.0f}   视频 {W}x{H}")

    out = Path(a.out)
    imgs = out / "images"
    labs = out / "labels"
    imgs.mkdir(parents=True, exist_ok=True)
    labs.mkdir(parents=True, exist_ok=True)
    stem0 = Path(a.video).stem

    made = 0
    for i in range(0, n, a.stride):
        if made >= a.max_frames:
            break
        cap.set(cv2.CAP_PROP_POS_FRAMES, i)
        ok, fr = cap.read()
        if not ok:
            break
        name = f"{stem0}_{i:06d}"
        cv2.imwrite(str(imgs / f"{name}.jpg"), fr,
                    [cv2.IMWRITE_JPEG_QUALITY, 92])
        if not a.only_frames:
            # 固定机位：所有抽样帧共用同一份框（这就是"点一次标几百张"的来源）
            line = "0 %.6f %.6f %.6f %.6f" % (cx / W, cy / H, bw / W, bh / H)
            (labs / f"{name}.txt").write_text(line + "\n", encoding="utf-8")
        made += 1
    cap.release()

    # data.yaml：单类 rim
    yml = out / "data.yaml"
    old = yml.read_text(encoding="utf-8") if yml.exists() else ""
    if "rim" not in old:
        yml.write_text(
            f"path: {out.resolve().as_posix()}\n"
            "train: images\nval: images\n"
            "names:\n  0: rim\n", encoding="utf-8")
    print(f"[ok] 生成 {made} 张 -> {imgs}（标注 {labs}）")
    print(f"[ok] data.yaml: {yml}")
    print("提示：换一段视频再跑一次（--out 用同一个），可以攒成多机位数据集")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())