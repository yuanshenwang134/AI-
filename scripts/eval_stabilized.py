"""稳像后的运动分析：解决"镜头一平移，运动块判据就崩"这个最大失败原因。

实测背景：
  * 固定机位素材：候选块 ~0.018 块/帧 → 判据可用；
  * 平移/剪接素材：2.2 块/帧（高 120 倍）→ 判出 41 个假进球（4 分钟 82 分）。
  根因：运动块用 `absdiff(前一帧, 当前帧)`，镜头一动**整幅画面都在动**。

本方法（稳像 → 再算运动）：
  1. 候选窗口内取中间帧为参考帧；
  2. 用 ORB+RANSAC 估计各帧到参考帧的单应矩阵（frame_motion）；
  3. 把各帧 warp 到参考帧坐标系 —— 背景被"钉住"，只剩球和球员在动；
  4. 在稳像后的画面上做球块检测 + 穿筐判据。

验证：用用户标注的 32 个候选（14 进球 / 18 没进）算精确率/召回，
并与"未稳像"的结果对比（未稳像最优 F1 只有 0.29）。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

import cv2                                                    # noqa: E402
import numpy as np                                            # noqa: E402

from aihoop.frame_motion import estimate_motion, warp_point    # noqa: E402
from aihoop.hoop import Hoop                                   # noqa: E402
from aihoop.hoopsight import (SightConfig, _blobs_in_window,   # noqa: E402
                              _judge_span, _pick_blob, _window)

VID = ROOT / "data" / "bili_nybo.mp4"
CANDS = ROOT / "out" / "label_bili" / "candidates.json"
FEEDBACK = ROOT / "data" / "basket_feedback.jsonl"
PAD_BEFORE, PAD_AFTER = 1.2, 0.4


def stabilized_chain(cap, fps, t0, t1, hoop, cfg, ref_t=None):
    """把 [t0,t1] 内的帧稳像到参考帧，返回 (球块链, 诊断)。"""
    win = _window(cfg, hoop, int(cap.get(cv2.CAP_PROP_FRAME_WIDTH)),
                  int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT)))
    x0, y0, x1, y1 = win
    W, H = x1 - x0, y1 - y0
    if W <= 8 or H <= 8:
        return [], {"error": "窗口太小"}
    ref_t = ref_t if ref_t is not None else (t0 + t1) / 2.0
    f0, f1 = int(t0 * fps), int(t1 * fps)
    stab, diag = [], {"n_frames": 0, "motion_ok": 0, "motion_fail": 0}
    prev_stab = None
    for f in range(f0, f1 + 1):
        t = f / fps
        cap.set(cv2.CAP_PROP_POS_FRAMES, f)
        ok, fr = cap.read()
        if not ok:
            continue
        diag["n_frames"] += 1
        # 估计"本帧 → 参考帧"的变换
        m = estimate_motion(str(VID), ref_t, t)
        if m["ok"] and m["H"] is not None:
            Hm = np.array(m["H"], dtype=np.float64)
            diag["motion_ok"] += 1
        else:
            Hm = np.eye(3)      # 估不出来就按不动处理（保守）
            diag["motion_fail"] += 1
        # 把整帧 warp 到参考帧，再裁篮筐窗口
        warped = cv2.warpPerspective(fr, Hm, (fr.shape[1], fr.shape[0]),
                                     flags=cv2.INTER_LINEAR,
                                     borderMode=cv2.BORDER_REPLICATE)
        crop = warped[y0:y1, x0:x1]
        if crop.size == 0:
            continue
        g = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY)
        if prev_stab is not None:
            blobs = _blobs_in_window(cv2, np, prev_stab, g,
                                     (0, 0, crop.shape[1], crop.shape[0]),
                                     hoop, cfg)
            b = _pick_blob(blobs, hoop, cfg)
            if b is not None:
                # 窗口内坐标 → 画面坐标（稳像后即参考帧坐标系）
                stab.append((round(t, 3), round(b[0] + x0, 1),
                             round(b[1] + y0, 1), int(b[2])))
        prev_stab = g
    return stab, diag


def main() -> int:
    d = json.loads(CANDS.read_text(encoding="utf-8"))
    cands = d["candidates"]
    fb = [json.loads(x) for x in
          FEEDBACK.read_text(encoding="utf-8").splitlines() if x.strip()]
    fb = [r for r in fb if "bili" in str(r.get("video", ""))]
    labels = {}
    for c in cands:
        for r in fb:
            if abs(float(r["t"]) - float(c["t0"])) < 1.0:
                labels[c["t0"]] = r["label"]
    cfg = SightConfig()
    cap = cv2.VideoCapture(str(VID))
    fps = cap.get(cv2.CAP_PROP_FPS)

    rows = []
    print(f"{'t0':>9} {'标注':>5} {'块数':>5} {'稳像OK':>6} {'判定':>6} {'置信':>6}")
    for c in cands:
        lab = labels.get(c["t0"])
        if not lab:
            continue
        hoop = Hoop(cx=c["hoop"][0], cy=c["hoop"][1], rx=c["hoop"][2],
                    ry=c["hoop"][3], votes=1, confidence=1.0, method="manual")
        t0 = max(0.0, float(c["t0"]) - PAD_BEFORE)
        t1 = float(c["t0"]) + PAD_AFTER
        chain, diag = stabilized_chain(cap, fps, t0, t1, hoop, cfg,
                                       ref_t=float(c["t0"]))
        shot = _judge_span(chain, hoop, cfg) if len(chain) >= 3 else None
        got = shot is not None
        rows.append({"t": float(c["t0"]), "label": lab, "got": got,
                     "n": len(chain), "conf": None if shot is None
                     else round(float(shot.confidence), 3),
                     "motion_ok": diag.get("motion_ok", 0),
                     "motion_fail": diag.get("motion_fail", 0)})
        print(f"{c['t0']:9.2f} {lab:>5} {len(chain):5d} "
              f"{diag.get('motion_ok', 0):6d} "
              f"{'进球' if got else '没进':>6} "
              f"{'-' if shot is None else format(shot.confidence, '.3f'):>6}")
    cap.release()

    tp = sum(1 for r in rows if r["got"] and r["label"] == "made")
    fp = sum(1 for r in rows if r["got"] and r["label"] == "miss")
    fn = sum(1 for r in rows if not r["got"] and r["label"] == "made")
    tn = sum(1 for r in rows if not r["got"] and r["label"] == "miss")
    prec = tp / (tp + fp) if tp + fp else 0.0
    rec = tp / (tp + fn) if tp + fn else 0.0
    f1 = 2 * prec * rec / (prec + rec) if prec + rec else 0.0
    print(f"\n=== 稳像后：精确率 {prec:.0%}  召回 {rec:.0%}  F1 {f1:.2f} "
          f"(TP{tp} FP{fp} FN{fn} TN{tn}) ===")
    print("（对比：未稳像时网格搜索最优 F1 只有 0.29、召回 19%）")
    (ROOT / "out" / "stab_eval.json").write_text(
        json.dumps({"rows": rows, "precision": prec, "recall": rec, "f1": f1,
                    "tp": tp, "fp": fp, "fn": fn, "tn": tn},
                   ensure_ascii=False, indent=2), encoding="utf-8")
    print("明细已存 out/stab_eval.json")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
