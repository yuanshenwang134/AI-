"""篮下分析的可复用工具：候选扫描、证据图、**画面标点**。

为什么单独抽出来：这几件事现在有三个使用场景，逻辑必须只有一份 ——
  1. `scripts/label_baskets.py`  人工判断「这一下到底进没进」；
  2. `scripts/mark_landmarks.py`  **在画面上点出篮筐/篮圈边界/球场角点**；
  3. 分析管线（`aihoop.cli video --hoop-hint`）读取标点结果，不再靠检测器赌。

标点为什么重要：这段素材（以及任何新机位）上，我的篮筐检测器和判据都是在
「猜」；用户点一次就能把「篮筐在哪、圈多大、攻哪边」变成已知量，
既省掉检测失败的风险，也让判据阈值（圈内/圈外）有真实尺度。
"""
from __future__ import annotations

import json
import math
import sys
from pathlib import Path
from typing import Optional

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))


def require_cv():
    import cv2
    import numpy as np
    return cv2, np


# ---------------------------------------------------------------------------
# 候选扫描：谁在篮筐附近出现过球
# ---------------------------------------------------------------------------
def scan_candidates(video: str, sight_cfg=None, hoop=None, hoop_track=None,
                    hoop_weights: str = "runs/detect/rim/weights/best.pt",
                    device: str = "0", min_chain: int = 3,
                    progress=None):
    """扫全片，返回 (候选列表, meta)。

    候选门槛刻意很松：只要篮筐窗口里出现「球的尺寸」的块就算。
    候选多不要紧（人工判一次很快），**漏了真进球才致命**。
    """
    from aihoop.hoop import Hoop, HoopConfig, HoopTrack, detect_hoop_track
    from aihoop.hoopsight import (_blobs_in_window, _pick_blob, _window,
                                  _visible_hoops, SightConfig)

    cv2, np = require_cv()
    cfg = sight_cfg or SightConfig()
    cfg.stride = 1

    if hoop is None:
        if hoop_track is None:
            hoop_track = detect_hoop_track(video, HoopConfig(),
                                           weights=hoop_weights, device=device,
                                           progress=progress)
        cap0 = cv2.VideoCapture(video)
        W0 = int(cap0.get(cv2.CAP_PROP_FRAME_WIDTH))
        H0 = int(cap0.get(cv2.CAP_PROP_FRAME_HEIGHT))
        cap0.release()
        hoops = _visible_hoops(hoop_track, W0, H0, cfg)
        if not hoops:
            raise RuntimeError("这段视频里没有定位到可用的篮筐；"
                               "用 scripts\\mark_landmarks.py 手动标一个")
        hoop, _samples = hoops[0]

    cap = cv2.VideoCapture(video)
    fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
    W = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    H = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    win = _window(cfg, hoop, W, H)

    prev = None
    series = []
    idx = 0
    while True:
        ok = cap.grab()
        if not ok:
            break
        ok, frame = cap.retrieve()
        if not ok:
            break
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        if prev is not None:
            blobs = _blobs_in_window(cv2, np, prev, gray, win, hoop, cfg)
            b = _pick_blob(blobs, hoop, cfg)
            if b is not None:
                series.append((round(idx / fps, 3), round(b[0], 1),
                               round(b[1], 1), int(b[2])))
        prev = gray
        idx += 1
    cap.release()

    gap = 3.0 / fps
    chains = []
    cur = [series[0]] if series else []
    for p in series[1:]:
        if p[0] - cur[-1][0] <= gap:
            cur.append(p)
        else:
            chains.append(cur)
            cur = [p]
    if cur:
        chains.append(cur)

    out = []
    for ch in chains:
        if len(ch) < min_chain:
            continue
        xs = [p[1] for p in ch]
        ys = [p[2] for p in ch]
        rel = [(x - hoop.cx) / max(1.0, hoop.rx) for x in xs]
        out.append({
            "t0": ch[0][0], "t1": ch[-1][0], "n": len(ch),
            "x_span": [round(min(xs), 1), round(max(xs), 1)],
            "y_span": [round(min(ys), 1), round(max(ys), 1)],
            "rel_x_min_abs": round(min(abs(r) for r in rel), 2),
            "rel_x_at_rim": round(min(rel, key=lambda r: abs(r)), 2),
            "drop_px": round(ys[-1] - ys[0], 1),
            "hoop": [round(hoop.cx, 1), round(hoop.cy, 1),
                     round(hoop.rx, 1), round(hoop.ry, 1)],
            "win": list(win),
            "chain": ch,
        })
    meta = {"hoop": [hoop.cx, hoop.cy, hoop.rx, hoop.ry],
            "window": list(win), "fps": fps, "blob_samples": len(series),
            "hoop_source": getattr(hoop, "method", "")}
    return out, meta


# ---------------------------------------------------------------------------
# 证据图
# ---------------------------------------------------------------------------
def make_sheet(video: str, cand: dict, out_png: Path, zoom: float = 2.6,
               ncols: int = 8) -> bool:
    """给一个候选出证据图（横排两行帧，黄圈=候选球块，红圈=篮圈）。"""
    cv2, np = require_cv()
    win = cand["win"]
    cx, cy, rx, ry = cand["hoop"]
    cap = cv2.VideoCapture(video)
    fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
    f0, f1 = int(max(0.0, cand["t0"] - 0.35) * fps), int((cand["t1"] + 0.55) * fps)
    step = max(1, (f1 - f0) // max(1, ncols * 2))
    tiles = []
    for f in range(f0, f1 + 1, step):
        cap.set(cv2.CAP_PROP_POS_FRAMES, f)
        ok, fr = cap.read()
        if not ok:
            continue
        crop = fr[win[1]:win[3], win[0]:win[2]]
        if crop.size == 0:
            continue
        big = cv2.resize(crop, None, fx=zoom, fy=zoom,
                         interpolation=cv2.INTER_LANCZOS4)
        cv2.ellipse(big, (int((cx - win[0]) * zoom), int((cy - win[1]) * zoom)),
                    (int(rx * zoom), int(ry * zoom)), 0, 0, 360, (0, 0, 255), 1)
        for (tt, bx, by, _area) in cand["chain"]:
            if abs(tt - f / fps) > 0.5 / fps:
                continue
            px, py = int((bx - win[0]) * zoom), int((by - win[1]) * zoom)
            cv2.circle(big, (px, py), 8, (0, 255, 255), 2)
            relx = (bx - cx) / max(1.0, rx)
            cv2.putText(big, f"x{relx:+.2f}", (px + 6, py),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.45, (0, 255, 255), 1)
        cv2.putText(big, f"{f / fps:.2f}s", (4, 18),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.5, (255, 255, 255), 1)
        tiles.append(big)
    cap.release()
    if not tiles:
        return False
    per = max(1, len(tiles) // 2)
    rows = []
    for i in range(0, len(tiles), per):
        chunk = tiles[i:i + per]
        hh = max(x.shape[0] for x in chunk)
        chunk = [cv2.copyMakeBorder(x, 0, hh - x.shape[0], 0, 0,
                                    cv2.BORDER_CONSTANT, value=(0, 0, 0))
                 for x in chunk]
        rows.append(np.hstack(chunk))
    ww = max(r.shape[1] for r in rows)
    rows = [cv2.copyMakeBorder(r, 0, 0, 0, ww - r.shape[1],
                               cv2.BORDER_CONSTANT, value=(0, 0, 0)) for r in rows]
    sheet = np.vstack(rows)
    head = np.zeros((34, sheet.shape[1], 3), np.uint8)
    cv2.putText(head, f"cand t={cand['t0']:.2f}~{cand['t1']:.2f}s  "
                      f"球心最近居圈心 {cand['rel_x_at_rim']:+.2f}rx  "
                      f"下落 {cand['drop_px']:.0f}px",
                (6, 23), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (255, 255, 255), 1)
    out_png.parent.mkdir(parents=True, exist_ok=True)
    cv2.imwrite(str(out_png), np.vstack([head, sheet]))
    return True


# ---------------------------------------------------------------------------
# 画面标点（交互）
# ---------------------------------------------------------------------------
LANDMARKS = [
    # (键, 名称, 提示)
    ("rim_center", "篮筐中心", "点篮圈中心（圆圈正中心）"),
    ("rim_left", "篮圈左缘", "点篮圈最左边"),
    ("rim_right", "篮圈右缘", "点篮圈最右边"),
    ("rim_top", "篮圈上沿", "点篮圈最上边"),
    ("rim_bottom", "篮圈下沿", "点篮圈最下边"),
]


def pick_landmarks(frame, title: str = "标点：左键点击 / 右键撤销 / N 跳过 / S 保存 / Q 退出",
                   zoom: float = 1.0, need=None):
    """在给定帧上交互标点，返回 {name: (x, y)}。

    做法：把整帧（或放大后的帧）显示出来，用户按顺序点；点完一个自动进入下一个。
    右键撤销上一个点，N 跳过当前项，S 保存退出，Q 放弃。
    """
    cv2, np = require_cv()
    img = frame.copy()
    if zoom != 1.0:
        img = cv2.resize(img, None, fx=zoom, fy=zoom,
                         interpolation=cv2.INTER_LANCZOS4)
    h, w = img.shape[:2]
    items = [it for it in LANDMARKS if (need is None or it[0] in need)]
    picked: dict[str, tuple[int, int]] = {}
    order = [it[0] for it in items]
    cur = 0

    def redraw():
        canvas = img.copy()
        # 已点的
        for name, (x, y) in picked.items():
            cv2.drawMarker(canvas, (x, y), (0, 255, 0), cv2.MARKER_CROSS, 26, 2)
            cv2.putText(canvas, name, (x + 8, y - 8),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0, 255, 0), 2)
        # 提示
        tip = " / ".join(
            (f"[{n}]" if n == (order[cur] if cur < len(order) else "") else n)
            for n in order)
        cv2.rectangle(canvas, (0, 0), (w, 46), (0, 0, 0), -1)
        cv2.putText(canvas, title, (8, 20), cv2.FONT_HERSHEY_SIMPLEX, 0.6,
                    (255, 255, 255), 1)
        now = order[cur] if cur < len(order) else None
        hint = next((it[2] for it in items if it[0] == now), "全部完成")
        cv2.putText(canvas, f"当前：{now} —— {hint}   进度 {tip}", (8, 40),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.55, (0, 255, 255), 1)
        return canvas

    def on_mouse(event, x, y, flags, param):
        nonlocal cur
        if event == cv2.EVENT_LBUTTONDOWN and cur < len(order):
            picked[order[cur]] = (x, y)
            cur += 1
            cv2.imshow("landmarks", redraw())
        elif event == cv2.EVENT_RBUTTONDOWN and cur > 0:
            cur -= 1
            picked.pop(order[cur], None)
            cv2.imshow("landmarks", redraw())

    cv2.namedWindow("landmarks", cv2.WINDOW_NORMAL)
    cv2.setMouseCallback("landmarks", on_mouse)
    cv2.imshow("landmarks", redraw())
    while True:
        k = cv2.waitKey(20) & 0xFF
        if k == 255:
            continue
        if k == ord('s'):
            break
        if k == ord('q'):
            picked = {}
            break
        if k == ord('n') and cur < len(order):
            cur += 1
            cv2.imshow("landmarks", redraw())
    cv2.destroyAllWindows()
    if zoom != 1.0:
        picked = {n: (int(round(x / zoom)), int(round(y / zoom)))
                  for n, (x, y) in picked.items()}
    return picked


def landmarks_to_hoop(marks: dict):
    """把标点换算成一个 Hoop(cx, cy, rx, ry)。

    优先用「中心 + 左右缘」定 rx、「中心 + 上下沿」定 ry；
    只给了中心也行（半径用画面尺寸的保守估计兜底）。
    """
    from aihoop.hoop import Hoop

    def g(name):
        v = marks.get(name)
        return (float(v[0]), float(v[1])) if v else None

    c = g("rim_center")
    if c is None:
        return None
    left, right = g("rim_left"), g("rim_right")
    top, bottom = g("rim_top"), g("rim_bottom")
    rx = 0.0
    if left and right:
        rx = abs(right[0] - left[0]) / 2.0
    elif left:
        rx = abs(c[0] - left[0])
    elif right:
        rx = abs(right[0] - c[0])
    ry = 0.0
    if top and bottom:
        ry = abs(bottom[1] - top[1]) / 2.0
    elif top:
        ry = abs(c[1] - top[1])
    elif bottom:
        ry = abs(bottom[1] - c[1])
    # 兜底：FIBA 篮圈 45cm、画面里按常见比例给个保守值，宁可小不要大
    rx = rx if rx >= 8 else 40.0
    ry = ry if ry >= 4 else rx * 0.42
    return Hoop(cx=c[0], cy=c[1], rx=rx, ry=ry, votes=1, confidence=1.0,
                method="manual-landmarks")


def save_marks(path: str, video: str, marks: dict, hoop=None,
               extra: Optional[dict] = None) -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    d = {"video": str(video), "landmarks": {k: list(v) for k, v in marks.items()},
         "created": __import__("time").time()}
    if hoop is not None:
        d["hoop"] = [hoop.cx, hoop.cy, hoop.rx, hoop.ry]
    if extra:
        d.update(extra)
    p.write_text(json.dumps(d, ensure_ascii=False, indent=2), encoding="utf-8")


def load_marks(path: str) -> Optional[dict]:
    p = Path(path)
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None
