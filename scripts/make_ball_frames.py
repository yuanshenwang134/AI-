"""生成「球位置标注」用的帧（给训练专用球检测器用）。

设计要点（决定了这条路能不能成）：
  * **不做全画面检测**：1920x1080 缩到 YOLO 的 640 后，7~15px 的球只剩 3~5px，
    根本学不到。所以只在**篮筐邻域的裁剪图**上检测 —— 而这正好是判进球所需的范围。
  * 裁剪 400x400 再放大 2 倍 → 800x800，球变成 ~20~30px，可学、可点。
  * 取帧位置贴着「用户确认的进球」和「候选窗口」，
    保证画面里球真的会出现（否则用户点半天都是空的）。

产出：
  data/ball_ds/frames/xxx.png   待标注的裁剪图（放大后）
  data/ball_ds/manifest.jsonl   每张的元信息（视频、帧号、裁剪框、来源）
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import cv2      # noqa: E402
import numpy as np  # noqa: E402

VID = ROOT / "data" / "bili_nybo.mp4"
MARKS = ROOT / "data" / "marks_bili_nybo.json"
CANDS = ROOT / "out" / "label_bili" / "candidates.json"
FEEDBACK = ROOT / "data" / "basket_feedback.jsonl"
OUT = ROOT / "data" / "ball_ds"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="生成球位置标注帧")
    ap.add_argument("--crop", type=int, default=400, help="裁剪边长")
    ap.add_argument("--zoom", type=float, default=2.0)
    ap.add_argument("--per-goal", type=int, default=10)
    ap.add_argument("--per-miss", type=int, default=4)
    ap.add_argument("--random", type=int, default=40)
    a = ap.parse_args(argv)

    mk = json.loads(MARKS.read_text(encoding="utf-8"))
    cx, cy = float(mk["hoop"][0]), float(mk["hoop"][1])
    fb = [json.loads(x) for x in
          FEEDBACK.read_text(encoding="utf-8").splitlines() if x.strip()]
    fb = [r for r in fb if "bili" in str(r.get("video", ""))]
    made = sorted(float(r["t"]) for r in fb if r["label"] == "made")
    miss = sorted(float(r["t"]) for r in fb if r["label"] == "miss")
    print(f"进球 {len(made)} 个 / 没进 {len(miss)} 个；篮筐 ({cx:.0f},{cy:.0f})")

    cap = cv2.VideoCapture(str(VID))
    fps = cap.get(cv2.CAP_PROP_FPS) or 24.0
    W = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    H = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
    dur = total / fps if fps else 0.0

    half = a.crop // 2
    x0 = int(max(0, min(W - a.crop, cx - half)))
    y0 = int(max(0, min(H - a.crop, cy - half)))
    print(f"裁剪框 x0,y0=({x0},{y0}) 边长 {a.crop} → 放大 {a.zoom:g}x "
          f"= {int(a.crop * a.zoom)}px")

    frames_dir = OUT / "frames"
    frames_dir.mkdir(parents=True, exist_ok=True)
    for f in frames_dir.glob("*.png"):
        f.unlink()

    rows = []

    def add(t: float, src: str, tag: str):
        fi = int(round(t * fps))
        cap.set(cv2.CAP_PROP_POS_FRAMES, max(0, fi))
        ok, fr = cap.read()
        if not ok:
            return
        crop = fr[y0:y0 + a.crop, x0:x0 + a.crop]
        if crop.size == 0:
            return
        big = cv2.resize(crop, None, fx=a.zoom, fy=a.zoom,
                         interpolation=cv2.INTER_LANCZOS4)
        name = f"{tag}_t{int(round(t * 1000)):08d}.png"
        cv2.imwrite(str(frames_dir / name), big)
        rows.append({"file": f"frames/{name}", "src": src, "t": round(t, 3),
                     "frame_idx": fi, "crop": [x0, y0, a.crop, a.crop],
                     "zoom": a.zoom, "video": str(VID),
                     # 篮筐在裁剪图（放大后）里的位置 —— 标注时用来参考
                     "hoop_in_crop": [round((cx - x0) * a.zoom, 1),
                                      round((cy - y0) * a.zoom, 1)]})

    # 进球时刻前后取帧（球在飞/刚入筐）
    for t in made:
        for k in range(a.per_goal):
            off = -0.45 + 0.75 * k / max(1, a.per_goal - 1)
            add(t + off, "goal", "goal")
    # 没进候选的窗口里取帧（含难例：球在筐附近但没进）
    for t in miss:
        for k in range(a.per_miss):
            off = -0.25 + 0.5 * k / max(1, a.per_miss - 1)
            add(t + off, "miss", "miss")
    # 随机时刻：当作"很可能没有球"的负样本（LED 反光等干扰物）
    rng = np.random.RandomState(3)
    made_all = set()
    for _ in range(a.random):
        t = float(rng.uniform(1.0, max(2.0, dur - 1.0)))
        if any(abs(t - m) < 2.0 for m in made):
            continue
        add(t, "random", "rand")
    cap.release()

    (OUT / "manifest.jsonl").write_text(
        "\n".join(json.dumps(r, ensure_ascii=False) for r in rows),
        encoding="utf-8")
    print(f"\n生成 {len(rows)} 张待标注图 → {frames_dir}")
    print(f"清单 {OUT / 'manifest.jsonl'}")
    print("\n下一步：python scripts\\label_ball.py")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
