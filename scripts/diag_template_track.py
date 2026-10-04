"""用用户标的 11 个球模板做**模板匹配跟踪**，看能不能自动得到真轨迹。

思路（为什么值得一试）：
  * 球的外观（橙棕色圆块）在一段视频里是稳定的，模板匹配不需要训练；
  * 只需要**筐口邻域**（400x400 放大 2 倍），球在里面 20~30px，匹配信噪比够；
  * 如果匹配出的轨迹连续、且朝篮筐下落 —— 就有了判进球所需的真实输入，
    用户不必再手点几百帧。

检验方式（不只看"匹配置信度高不高"，那容易自欺）：
  1. 对 14 个进球时刻：轨迹是否**连续**（帧间位移小）且**朝筐心汇聚**；
  2. 对没进时刻：轨迹是否**不穿过圈内**；
  3. 打印每帧的匹配分数与位置，并出叠加图便于肉眼核对。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
import cv2      # noqa: E402
import numpy as np  # noqa: E402

DS = ROOT / "data" / "ball_ds"
VID = ROOT / "data" / "bili_nybo.mp4"
MARKS = json.loads((ROOT / "data/marks_bili_nybo.json").read_text(encoding="utf-8"))
CROP = 400
ZOOM = 2.0


def load_templates():
    """从已标注的帧里取球模板（放大图坐标 → 原图坐标 → 灰度模板）。"""
    state = [json.loads(l) for l in
             (DS / "labeled.jsonl").read_text(encoding="utf-8").splitlines()
             if l.strip()]
    tpls = []
    for st in state:
        if st.get("status") != "ball" or not st.get("box"):
            continue
        img = cv2.imread(str(DS / st["file"]), cv2.IMREAD_GRAYSCALE)
        if img is None:
            continue
        bx, by, bw, bh = st["box"]
        x0 = int(max(0, bx - bw / 2)); y0 = int(max(0, by - bh / 2))
        x1 = int(min(img.shape[1], bx + bw / 2))
        y1 = int(min(img.shape[0], by + bh / 2))
        tpl = img[y0:y1, x0:x1]
        if tpl.size < 64:
            continue
        tpls.append({"tpl": tpl, "t": st.get("t"), "src": st["src"]})
    return tpls


def track(cap, fps, t_center, tpls, span=(-0.9, 0.35), margin=60):
    """在 t_center 前后逐帧做多尺度模板匹配，返回轨迹。"""
    hoop = MARKS["hoop"]
    cx, cy = float(hoop[0]), float(hoop[1])
    half = CROP // 2
    x0 = int(max(0, min(1920 - CROP, cx - half)))
    y0 = int(max(0, min(1080 - CROP, cy - half)))
    f0, f1 = int((t_center + span[0]) * fps), int((t_center + span[1]) * fps)
    traj = []
    for f in range(f0, f1 + 1):
        cap.set(cv2.CAP_PROP_POS_FRAMES, max(0, f))
        ok, fr = cap.read()
        if not ok:
            continue
        crop = fr[y0:y0 + CROP, x0:x0 + CROP]
        big = cv2.resize(crop, None, fx=ZOOM, fy=ZOOM,
                         interpolation=cv2.INTER_LANCZOS4)
        g = cv2.cvtColor(big, cv2.COLOR_BGR2GRAY)
        best = None
        for t in tpls:
            tpl = t["tpl"]
            for s in (0.7, 0.85, 1.0, 1.2, 1.45):
                tw, th = int(tpl.shape[1] * s), int(tpl.shape[0] * s)
                if tw < 6 or th < 6 or tw >= g.shape[1] or th >= g.shape[0]:
                    continue
                tt = cv2.resize(tpl, (tw, th), interpolation=cv2.INTER_AREA)
                r = cv2.matchTemplate(g, tt, cv2.TM_CCOEFF_NORMED)
                _mn, mx, _ml, ml = cv2.minMaxLoc(r)
                if best is None or mx > best[0]:
                    best = (mx, ml[0] + tw / 2, ml[1] + th / 2, tw)
        if best:
            traj.append({"f": f, "t": f / fps, "score": round(best[0], 3),
                         "x": round(best[1], 1), "y": round(best[2], 1),
                         "w": best[3]})
    return traj, (x0, y0)


def summarize(traj, hoop_px):
    """轨迹质量：连续性与是否朝筐心。"""
    if len(traj) < 6:
        return {"n": len(traj), "note": "帧太少"}
    xs = np.array([p["x"] for p in traj]); ys = np.array([p["y"] for p in traj])
    sc = np.array([p["score"] for p in traj])
    hop = np.hypot(np.diff(xs), np.diff(ys))
    hx = (hoop_px[0] - traj[0]["x"]) if False else None
    return {"n": len(traj), "score_med": round(float(np.median(sc)), 3),
            "score_min": round(float(sc.min()), 3),
            "jump_med": round(float(np.median(hop)), 1),
            "jump_max": round(float(hop.max()), 1),
            "x_range": [round(float(xs.min()), 1), round(float(xs.max()), 1)],
            "y_span": round(float(ys.max() - ys.min()), 1)}


def main() -> int:
    tpls = load_templates()
    print(f"模板 {len(tpls)} 个（来自用户标注）")
    if len(tpls) < 3:
        print("[err] 模板太少（<3），先多标几帧")
        return 2
    for t in tpls[:3]:
        print(f"  模板 t={t['t']} 尺寸 {t['tpl'].shape}")

    fb = [json.loads(l) for l in
          (ROOT / "data/basketball_match.mp4").parent.joinpath(
              "basket_feedback.jsonl").read_text(encoding="utf-8").splitlines()
          if l.strip()]
    fb = [r for r in fb if "bili" in str(r.get("video", ""))]
    made = sorted(float(r["t"]) for r in fb if r["label"] == "made")
    miss = sorted(float(r["t"]) for r in fb if r["label"] == "miss")

    cap = cv2.VideoCapture(str(VID))
    fps = cap.get(cv2.CAP_PROP_FPS) or 24.0
    hoop = MARKS["hoop"]
    # 篮筐在放大裁剪图里的位置
    half = CROP // 2
    x0 = int(max(0, min(1920 - CROP, float(hoop[0]) - half)))
    y0 = int(max(0, min(1080 - CROP, float(hoop[1]) - half)))
    hp = ((float(hoop[0]) - x0) * ZOOM, (float(hoop[1]) - y0) * ZOOM)

    print("\n=== 14 个进球时刻的模板跟踪质量 ===")
    res = []
    for t in made[:6]:
        traj, _xy = track(cap, fps, t, tpls)
        s = summarize(traj, hp)
        res.append((t, s, traj))
        print(f"  t={t:8.2f}s  n={s.get('n')}  匹配中位分 {s.get('score_med')} "
              f"(最低 {s.get('score_min')})  帧间跳变中位 {s.get('jump_med')}px "
              f"最大 {s.get('jump_max')}px  y 跨度 {s.get('y_span')}px")
    print("\n=== 没进时刻（对照）===")
    for t in miss[:3]:
        traj, _xy = track(cap, fps, t, tpls)
        s = summarize(traj, hp)
        print(f"  t={t:8.2f}s  n={s.get('n')}  匹配中位分 {s.get('score_med')} "
              f"帧间跳变中位 {s.get('jump_med')}px  y 跨度 {s.get('y_span')}px")
    cap.release()

    # 出图核对第一个进球
    if res:
        t, _s, traj = res[0]
        cap = cv2.VideoCapture(str(VID))
        tiles = []
        for p in traj[::2][:14]:
            cap.set(cv2.CAP_PROP_POS_FRAMES, p["f"])
            ok, fr = cap.read()
            if not ok:
                continue
            crop = fr[y0:y0 + CROP, x0:x0 + CROP]
            big = cv2.resize(crop, None, fx=ZOOM, fy=ZOOM,
                             interpolation=cv2.INTER_LANCZOS4)
            cv2.ellipse(big, (int(hp[0]), int(hp[1])),
                        (int(float(hoop[2]) * ZOOM), int(float(hoop[3]) * ZOOM)),
                        0, 0, 360, (0, 0, 255), 2)
            cv2.circle(big, (int(p["x"]), int(p["y"])), 16, (0, 255, 0), 2)
            cv2.putText(big, f"{p['t']:.2f} {p['score']:.2f}", (8, 26),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.7, (255, 255, 255), 2)
            tiles.append(big)
        cap.release()
        if tiles:
            rows = []
            for k in range(0, len(tiles), 5):
                ch = tiles[k:k + 5]
                hh = max(x.shape[0] for x in ch)
                ch = [cv2.copyMakeBorder(x, 0, hh - x.shape[0], 0, 0,
                                         cv2.BORDER_CONSTANT, value=(0, 0, 0))
                      for x in ch]
                rows.append(np.hstack(ch))
            ww = max(r.shape[1] for r in rows)
            rows = [cv2.copyMakeBorder(r, 0, 0, 0, ww - r.shape[1],
                                       cv2.BORDER_CONSTANT, value=(0, 0, 0))
                    for r in rows]
            out = ROOT / "out" / f"track_{int(t)}.png"
            cv2.imwrite(str(out), np.vstack(rows))
            print(f"\n叠加图 → {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
