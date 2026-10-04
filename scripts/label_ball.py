"""球位置标注工具：在放大图上点一下球心，就完成一帧。

用法：
    python scripts\\label_ball.py                 # 从头/继续
    python scripts\\label_ball.py --redo          # 清空重标

键鼠：
    左键    在球心点一下（框大小用 +/- 或滚轮调）
    N       这帧里没有球（当负样本 —— LED 反光这类干扰物很有用）
    U       看不清，跳过
    B       回上一帧
    S       保存并退出
    Q       不保存退出

产出（YOLO 单类 ball）：
    data/ball_ds/labels/<同名>.txt   "0 cx cy w h"（归一化到放大图坐标）
    data/ball_ds/labeled.jsonl       每帧状态（ball / none / skip）
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
import cv2      # noqa: E402
import numpy as np  # noqa: E402

DS = ROOT / "data" / "ball_ds"
MANIFEST = DS / "manifest.jsonl"
LABELS = DS / "labels"
STATE = DS / "labeled.jsonl"


def load_state() -> dict:
    if not STATE.exists():
        return {}
    out = {}
    for line in STATE.read_text(encoding="utf-8").splitlines():
        if line.strip():
            d = json.loads(line)
            out[d["file"]] = d
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="球位置标注")
    ap.add_argument("--redo", action="store_true", help="清空已有标注重新开始")
    ap.add_argument("--start", type=int, default=0, help="从第几张开始")
    a = ap.parse_args(argv)

    rows = [json.loads(x) for x in
            MANIFEST.read_text(encoding="utf-8").splitlines() if x.strip()]
    LABELS.mkdir(parents=True, exist_ok=True)
    state = {} if a.redo else load_state()
    if a.redo:
        for f in LABELS.glob("*.txt"):
            f.unlink()
        if STATE.exists():
            STATE.unlink()
    done = sum(1 for r in rows if r["file"] in state
               and state[r["file"]].get("status") in ("ball", "none"))
    print(f"共 {len(rows)} 张，已标 {done} 张")
    print("左键=点球心  N=没有球  U=看不清  B=上一张  S=保存退出  Q=放弃")
    print("+/- 或滚轮 = 调框大小（球在放大图里通常 14~30px）")

    size = 24
    i = max(0, a.start)
    cv2.namedWindow("label_ball", cv2.WINDOW_NORMAL)
    cv2.resizeWindow("label_ball", 900, 980)

    ctx = {"mouse": (0, 0), "size": 24, "click": None}

    def on_event(event, x, y, flags, param):
        if event == cv2.EVENT_MOUSEMOVE:
            param["mouse"] = (x, y)
        elif event == cv2.EVENT_MOUSEWHEEL:
            param["size"] = max(8, min(80, param["size"] + (2 if flags > 0 else -2)))
        elif event == cv2.EVENT_LBUTTONDOWN:
            param["click"] = (x, y)

    cv2.setMouseCallback("label_ball", on_event, ctx)

    saved = 0
    while 0 <= i < len(rows):
        r = rows[i]
        size = ctx["size"]
        img = cv2.imread(str(DS / r["file"]))
        if img is None:
            i += 1
            continue
        st = state.get(r["file"], {})
        show = img.copy()
        H, W = show.shape[:2]
        # 篮筐参考位置
        hx, hy = r.get("hoop_in_crop", [0, 0])
        cv2.drawMarker(show, (int(hx), int(hy)), (0, 0, 255),
                       cv2.MARKER_CROSS, 18, 2)
        # 鼠标预览框
        mx, my = ctx["mouse"]
        cv2.rectangle(show, (mx - size // 2, my - size // 2),
                      (mx + size // 2, my + size // 2), (0, 255, 255), 1)
        # 已标过的显示
        if st.get("status") == "ball" and st.get("box"):
            bx, by, bw, bh = st["box"]
            cv2.rectangle(show, (int(bx - bw / 2), int(by - bh / 2)),
                          (int(bx + bw / 2), int(by + bh / 2)), (0, 255, 0), 2)
        bar = np.zeros((58, W, 3), np.uint8)
        left = len(rows) - i
        cv2.putText(bar, f"[{i + 1}/{len(rows)}] 剩 {left}   来源={r['src']} "
                         f"t={r['t']:.2f}s   框={size}px   状态={st.get('status', '未标')}",
                    (8, 22), cv2.FONT_HERSHEY_SIMPLEX, 0.62, (255, 255, 255), 1)
        cv2.putText(bar, "左键=球心  N=没有球  U=看不清  B=上一张  S=保存  Q=退出  +/-=框大小",
                    (8, 46), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0, 255, 255), 1)
        cv2.imshow("label_ball", np.vstack([show, bar]))
        # 处理点击
        if ctx["click"] is not None:
            px, py = ctx["click"]
            ctx["click"] = None
            if py < H:
                st = {"file": r["file"], "status": "ball",
                      "box": [float(px), float(py), float(size), float(size)],
                      "t": r["t"], "src": r["src"]}
                state[r["file"]] = st
                saved += 1
                i += 1
                continue
        k = cv2.waitKey(30) & 0xFF
        if k == 255:
            continue
        if k in (ord('n'), ord('N')):
            state[r["file"]] = {"file": r["file"], "status": "none",
                                "t": r["t"], "src": r["src"]}
            saved += 1
            i += 1
        elif k in (ord('u'), ord('U')):
            state[r["file"]] = {"file": r["file"], "status": "skip",
                                "t": r["t"], "src": r["src"]}
            i += 1
        elif k in (ord('b'), ord('B')):
            i = max(0, i - 1)
        elif k in (ord('+'), ord('=')):
            ctx["size"] = min(80, ctx["size"] + 2)
        elif k in (ord('-'), ord('_')):
            ctx["size"] = max(8, ctx["size"] - 2)
        elif k in (ord('s'), ord('S')):
            break
        elif k in (ord('q'), ord('Q')):
            cv2.destroyAllWindows()
            print("已放弃保存")
            return 1
    cv2.destroyAllWindows()

    # 写 YOLO 标签 + 状态
    n_ball = n_none = n_skip = 0
    for f, st in state.items():
        img_p = DS / f
        if not img_p.exists():
            continue
        im = cv2.imread(str(img_p))
        if im is None:
            continue
        H, W = im.shape[:2]
        stem = Path(f).stem
        if st["status"] == "ball":
            bx, by, bw, bh = st["box"]
            with open(LABELS / f"{stem}.txt", "w", encoding="utf-8") as fh:
                fh.write(f"0 {bx / W:.6f} {by / H:.6f} {bw / W:.6f} {bh / H:.6f}\n")
            n_ball += 1
        elif st["status"] == "none":
            (LABELS / f"{stem}.txt").write_text("", encoding="utf-8")
            n_none += 1
        else:
            n_skip += 1
    STATE.write_text("\n".join(json.dumps(v, ensure_ascii=False)
                               for v in state.values()), encoding="utf-8")
    print(f"\n已保存：有球 {n_ball} 张，明确没球 {n_none} 张，跳过 {n_skip} 张")
    print(f"标签目录 {LABELS}")
    print(f"状态文件 {STATE}")
    print("\n下一步：python scripts\\train_ball_detector.py")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
