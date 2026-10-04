"""把候选时刻切成**放大的短视频**（给人工判进球用）。

为什么不用逐帧拼图：用户看不懂 —— 静态拼图要自己去比对帧间位置。
视频 + 慢放 + 放大，球有没有从圈里落下去是一眼的事。

做法：
  1. cv2 按需要裁「篮筐邻域」并放大，写上时刻、画上篮圈椭圆与球块标记；
  2. 帧写进临时 AVI（MJPG），再用 ffmpeg 转 H.264（浏览器才肯播）；
  3. 慢放 = 每帧重复写 N 次（不依赖 ffmpeg 的变速参数，简单可控）。

用法：
    python scripts/make_basket_clip.py --video data/bili_nybo.mp4 `
        --t 1965.88 --win 0,0,300,200 --zoom 4 --slow 3 --out out/x.mp4
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))


def find_ffmpeg() -> str:
    local = ROOT / "ffmpeg.exe"
    if local.exists():
        return str(local)
    return "ffmpeg"


def make_clip(video: str, t0: float, t1: float, win, zoom: float,
              slow: int, out_mp4: Path, hoop_px=None, chain=None,
              pad_before: float = 1.6, pad_after: float = 1.0,
              fps_out: int = 24) -> bool:
    import cv2
    import numpy as np

    out_mp4.parent.mkdir(parents=True, exist_ok=True)
    cap = cv2.VideoCapture(str(video))
    if not cap.isOpened():
        return False
    fps = cap.get(cv2.CAP_PROP_FPS) or 24.0
    start = max(0.0, float(t0) - pad_before)
    end = float(t1) + pad_after
    f0, f1 = int(start * fps), int(end * fps)
    x0, y0, x1, y1 = [int(v) for v in win]
    W, H = x1 - x0, y1 - y0
    if W <= 4 or H <= 4:
        cap.release()
        return False
    ow, oh = int(W * zoom), int(H * zoom)
    # H.264 要求宽高是偶数
    ow, oh = ow - (ow % 2), oh - (oh % 2)

    tmp_avi = Path(tempfile.gettempdir()) / f"_clip_{out_mp4.stem}.avi"
    vw = cv2.VideoWriter(str(tmp_avi), cv2.VideoWriter_fourcc(*"MJPG"),
                         30.0, (ow, oh))
    if not vw.isOpened():
        cap.release()
        return False

    def chain_at(t):
        if not chain:
            return None
        best, bd = None, 1e9
        for p in chain:
            d = abs(float(p[0]) - t)
            if d < bd:
                best, bd = p, d
        return best if (best and bd <= 1.2 / fps) else None

    n = 0
    for f in range(f0, f1 + 1):
        cap.set(cv2.CAP_PROP_POS_FRAMES, f)
        ok, fr = cap.read()
        if not ok:
            continue
        t = f / fps
        crop = fr[max(0, y0):y0 + H, max(0, x0):x0 + W]
        if crop.size == 0:
            continue
        big = cv2.resize(crop, (ow, oh), interpolation=cv2.INTER_LANCZOS4)
        # 篮圈
        if hoop_px:
            cx, cy, rx, ry = [float(v) for v in hoop_px[:4]]
            cv2.ellipse(big, (int((cx - x0) * zoom), int((cy - y0) * zoom)),
                        (max(3, int(rx * zoom)), max(2, int(ry * zoom))),
                        0, 0, 360, (0, 0, 255), 2)
        # 球块（候选链）
        p = chain_at(t)
        if p:
            bx, by = float(p[1]), float(p[2])
            if x0 <= bx <= x1 and y0 <= by <= y1:
                cv2.circle(big, (int((bx - x0) * zoom), int((by - y0) * zoom)),
                           14, (0, 255, 255), 3)
        # 时刻（大字，便于对时）
        cv2.putText(big, f"{t:.2f}s", (10, 34), cv2.FONT_HERSHEY_SIMPLEX,
                    1.0, (255, 255, 255), 2)
        for _ in range(max(1, int(slow))):
            vw.write(big)
            n += 1
    vw.release()
    cap.release()
    if n == 0:
        return False
    # MJPG → H.264（浏览器才播）
    cmd = [find_ffmpeg(), "-y", "-loglevel", "error", "-i", str(tmp_avi),
           "-c:v", "libx264", "-preset", "veryfast", "-crf", "24",
           "-pix_fmt", "yuv420p", "-an", "-movflags", "+faststart",
           str(out_mp4)]
    try:
        r = subprocess.run(cmd, capture_output=True, timeout=300)
        if r.returncode != 0:
            # 没有 ffmpeg 就退化：直接把 MJPG 的 avi 复制过去（多数浏览器也能放）
            import shutil
            shutil.copy(str(tmp_avi), str(out_mp4.with_suffix(".avi")))
            return False
    finally:
        try:
            tmp_avi.unlink()
        except OSError:
            pass
    return out_mp4.exists() and out_mp4.stat().st_size > 1000


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="切放大的候选短视频")
    ap.add_argument("--video", required=True)
    ap.add_argument("--t", type=float, required=True)
    ap.add_argument("--t1", type=float, default=None)
    ap.add_argument("--win", required=True, help="x0,y0,x1,y1")
    ap.add_argument("--zoom", type=float, default=4.0)
    ap.add_argument("--slow", type=int, default=3)
    ap.add_argument("--hoop", default=None, help="cx,cy,rx,ry")
    ap.add_argument("--out", required=True)
    a = ap.parse_args(argv)
    win = [int(float(v)) for v in a.win.split(",")]
    hoop = [float(v) for v in a.hoop.split(",")] if a.hoop else None
    ok = make_clip(a.video, a.t, a.t1 if a.t1 is not None else a.t + 0.4,
                   win, a.zoom, a.slow, Path(a.out), hoop_px=hoop)
    print("ok" if ok else "failed", a.out)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
