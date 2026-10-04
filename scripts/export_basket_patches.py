"""导出「篮筐切片」训练数据 —— 让进球判定可以**学**，而不是靠手调阈值。

为什么要做成学习问题：
  手写判据在 nybo_3min 上 precision = 0%（3 个判定全错），而且怎么调阈值都救不了。
  根因是这个机位篮筐贴画面顶边，"球从上方落下"没有判别信息。要突破就得让模型
  从数据里学"进球长什么样"，而不是我猜规则。

数据从哪来（关键是**正样本**）：
  * `basketball_match.mp4` 有比分牌 → 比分牌逐次得分事件 = **真值正样本时刻**；
    其余时刻里篮筐附近有球块运动的 = 负样本。篮筐位置由球场标定反算得到
    （`(0,±1.575)` → 像素），不需要篮筐检测器。
  * `nybo_3min.mp4` 有用户标注（7 个候选全是"没进"）→ **外部测试集**：
    模型在训练素材上学到的东西，拿到这段上应该判"没进球"，这才是检验。

导出内容（YOLO/自定义训练都能用）：
  out/basket_ds/
    meta.jsonl          每行：{split, video, t, hoop_px, label, patch0, patch1, feats}
    patches/<key>.npy   两张相隔 N 帧的篮筐邻域图（灰度，固定尺寸）
    features.jsonl      纯数值特征（网动能量比、球块下落…）—— 给"小数据"备选
用法：
    python scripts/export_basket_patches.py
    python scripts/train_basket_model.py --ds out/basket_ds
"""
from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

PATCH = 96          # 切片边长（像素，会从原始帧裁 96x96）
ZOOM = 1            # 再放大倍数（存成 96*ZOOM）
GAP_S = 0.12        # 两张图的时间间隔（球穿筐只要 ~0.1s，两张够看运动）


def _require_cv():
    import cv2
    import numpy as np
    return cv2, np


def hoop_px_from_cal(cal, half_left=True):
    """用球场标定反算篮筐 (0,±1.575) 的像素位置。"""
    import numpy as np
    H = np.array(cal.H, dtype=float)
    Hi = np.linalg.inv(H)

    def to_px(x, y):
        v = Hi @ np.array([x, y, 1.0])
        return float(v[0] / v[2]), float(v[1] / v[2])

    # 两个篮筐都在画面内的取靠前的那个；只有一个在画面里就用它
    cands = [to_px(0.0, -1.575), to_px(0.0, 1.575)]
    return cands


def net_features(cv2, np, g0, g1, cx, cy, rx, ry):
    """两张灰度帧 → 篮筐邻域的数值特征（用于没有 GPU 的小数据方案）。"""
    H, W = g0.shape
    yy, xx = np.mgrid[0:H, 0:W]
    dx = (xx - cx) / max(1.0, rx)
    dy = (yy - cy) / max(1.0, ry)
    r = np.sqrt(dx ** 2 + dy ** 2)
    net = (r <= 1.0) & (yy >= cy - ry * 0.2) & (yy <= cy + ry * 3.0)
    ring = (r > 1.0) & (r <= 1.9) & (yy <= cy + ry * 3.0)
    d = cv2.absdiff(g1, g0)
    db = cv2.GaussianBlur(d, (5, 5), 0)
    en = float(db[net].mean()) if net.any() else 0.0
    er = float(db[ring].mean()) if ring.any() else 0.0
    # 球块（小运动块）在筐内的横向偏移与下落
    m = (d >= 18).astype(np.uint8) * 255
    m = cv2.morphologyEx(m, cv2.MORPH_OPEN, np.ones((3, 3), np.uint8))
    n, _lab, stats, cent = cv2.connectedComponentsWithStats(m, 8)
    exp_a = math.pi * (rx * 0.55) ** 2
    best = None
    for i in range(1, n):
        a = int(stats[i, 4])
        if a < max(6, exp_a * 0.12) or a > exp_a * 9:
            continue
        cx0, cy0 = float(cent[i][0]), float(cent[i][1])
        dd = abs(cx0 - cx) / max(1.0, rx)
        if best is None or dd < best[0]:
            best = (dd, cy0, a)
    return {
        "net_energy": round(en, 2), "ring_energy": round(er, 2),
        "net_ratio": round(en / max(0.05, er), 2),
        "ball_relx": (None if best is None else round(best[0], 2)),
        "ball_cy_rel": (None if best is None
                        else round((best[1] - cy) / max(1.0, ry), 2)),
        "ball_area": (None if best is None else int(best[2])),
        "blob_count": int(n - 1),
    }


def export_patch(cv2, np, cap, fps, t, cx, cy, out_npy: Path):
    """裁两张相隔 GAP_S 的篮筐邻域图，存成 npy（灰度）。

    返回 (patch_stack, (x0, y0), (full_g0, full_g1))：后两个是**整帧灰度**，
    用来算数值特征（球块检测需要原分辨率，96x96 的切片太小）。
    """
    half = PATCH // 2
    imgs, fulls = [], []
    for off in (0.0, GAP_S):
        f = int(round((t + off) * fps))
        cap.set(cv2.CAP_PROP_POS_FRAMES, max(0, f))
        ok, fr = cap.read()
        if not ok:
            return None, None, None
        g = cv2.cvtColor(fr, cv2.COLOR_BGR2GRAY)
        fulls.append(g)
        H, W = g.shape
        x0 = int(max(0, min(W - PATCH, cx - half)))
        y0 = int(max(0, min(H - PATCH, cy - half)))
        if x0 < 0 or y0 < 0:
            return None, None, None
        patch = g[y0:y0 + PATCH, x0:x0 + PATCH]
        if patch.shape != (PATCH, PATCH):
            return None, None, None
        imgs.append(patch)
    arr = np.stack(imgs, axis=0).astype(np.uint8)     # (2, PATCH, PATCH)
    out_npy.parent.mkdir(parents=True, exist_ok=True)
    np.save(out_npy, arr)
    return arr, (x0, y0), (fulls[0], fulls[1])


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="导出篮筐切片训练数据")
    ap.add_argument("--out", default="out/basket_ds")
    ap.add_argument("--neg-stride", type=float, default=1.0,
                    help="在训练素材上每隔多少秒取一个负样本")
    ap.add_argument("--max-neg", type=int, default=240)
    ap.add_argument("--match-hoop", default="1075,105",
                    help="训练素材里篮筐的像素坐标 cx,cy。**必须给对** —— "
                         "用球场标定反算不可靠（实测那份标定把篮筐算到了比分牌"
                         "覆盖层上，等于训练模型认记分条）")
    args = ap.parse_args(argv)

    cv2, np = _require_cv()
    from aihoop.court import Calibration

    out = Path(args.out)
    (out / "patches").mkdir(parents=True, exist_ok=True)
    rows = []

    # ---------- 1) 训练素材：有比分牌真值的转播片段 ----------
    match_video = ROOT / "data" / "basketball_match.mp4"
    cal_path = ROOT / "data" / "calibration.backup2.json"
    ref_path = ROOT / "out" / "ref_scoreboard_match.json"
    if match_video.exists() and ref_path.exists():
        ref = json.loads(ref_path.read_text(encoding="utf-8"))
        events = [e for e in (ref.get("events") or [])
                  if e.get("kind") == "score"]
        cap = cv2.VideoCapture(str(match_video))
        fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
        W = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
        Hh = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
        # 篮筐坐标**必须给对**。踩过的坑：用球场标定反算 (0,±1.575) 得到
        # (639,672)，而那其实是**比分牌覆盖层**的位置（标定本身是错的）——
        # 于是训练出来的模型学的是"记分条变数字的瞬间"，AUC 看着 0.89 但毫无意义。
        # 现在改成从命令行给实测坐标（默认值是在画面上量出来的远端篮筐）。
        try:
            cx, cy = [float(x) for x in str(args.match_hoop).split(",")]
        except Exception:
            print(f"[err] --match-hoop 格式应为 cx,cy，收到 {args.match_hoop!r}")
            return 2
        # 校验：这个位置不能是比分牌覆盖层（覆盖层在画面下方中央）
        if Hh and cy > Hh * 0.75 and 0.3 * W < cx < 0.75 * W:
            print(f"[warn] 篮筐位置 ({cx:.0f},{cy:.0f}) 落在画面下方中央 —— "
                  "那通常是比分牌覆盖层，不是篮筐。请核对 --match-hoop。")
        hoop = (cx, cy)
        print(f"训练素材 {match_video.name}: 篮筐像素 {hoop}, "
              f"比分牌得分事件 {len(events)} 次")
        if hoop:
            rx = max(20.0, PATCH * 0.28)
            ry = rx * 0.42
            # 正样本：比分牌报的得分时刻（略微提前一点，球刚落筐）
            for k, e in enumerate(events):
                t = max(0.0, float(e["t"]) - 0.6)
                key = f"match_pos_{k:03d}"
                arr, xy, fulls = export_patch(cv2, np, cap, fps, t, cx, cy,
                                              out / "patches" / f"{key}.npy")
                if arr is None:
                    continue
                feats = net_features(cv2, np, fulls[0], fulls[1], cx, cy, rx, ry)
                rows.append({"split": "train", "key": key, "video": str(match_video),
                             "t": round(t, 2), "hoop_px": [cx, cy],
                             "label": 1, "src": "scoreboard",
                             "patch": f"patches/{key}.npy", **feats})
            # 负样本：其他时刻（每隔 neg-stride 秒）
            total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
            dur = total / fps if fps else 0.0
            used = 0
            t = 2.0
            pos_t = [float(e["t"]) for e in events]
            while t < dur and used < args.max_neg:
                if all(abs(t - p) > 4.0 for p in pos_t):
                    key = f"match_neg_{used:03d}"
                    arr, xy, fulls = export_patch(cv2, np, cap, fps, t, cx, cy,
                                                  out / "patches" / f"{key}.npy")
                    if arr is not None:
                        feats = net_features(cv2, np, fulls[0], fulls[1],
                                             cx, cy, rx, ry)
                        rows.append({"split": "train", "key": key,
                                     "video": str(match_video), "t": round(t, 2),
                                     "hoop_px": [cx, cy], "label": 0,
                                     "src": "sampled", "patch": f"patches/{key}.npy",
                                     **feats})
                        used += 1
                t += args.neg_stride
            print(f"  → 训练样本：正 {sum(1 for r in rows if r['label'] == 1)} / "
                  f"负 {sum(1 for r in rows if r['label'] == 0)}")
        cap.release()
    else:
        print("缺少训练素材（basketball_match.mp4 / 标定 / 比分牌事件），跳过")

    # ---------- 2) 测试素材：nybo（用户标注：全部没进） ----------
    nybo = ROOT / "data" / "nybo_3min.mp4"
    cand_file = ROOT / "out" / "label_nybo" / "candidates.json"
    if nybo.exists() and cand_file.exists():
        data = json.loads(cand_file.read_text(encoding="utf-8"))
        cap = cv2.VideoCapture(str(nybo))
        fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
        for c in data["candidates"]:
            cx, cy = c["hoop"][0], c["hoop"][1]
            key = f"nybo_t{int(c['t0'] * 100):05d}"
            t = c["t0"]
            arr, xy, fulls = export_patch(cv2, np, cap, fps, t, cx, cy,
                                          out / "patches" / f"{key}.npy")
            if arr is None:
                continue
            feats = net_features(cv2, np, fulls[0], fulls[1], cx, cy,
                                 max(1.0, c["hoop"][2]), max(1.0, c["hoop"][3]))
            rows.append({"split": "test", "key": key, "video": str(nybo),
                         "t": round(t, 2), "hoop_px": [cx, cy], "label": 0,
                         "src": "human_label_all_miss",
                         "patch": f"patches/{key}.npy", **feats})
        cap.release()
        print(f"测试素材 nybo：{sum(1 for r in rows if r['split'] == 'test')} 个样本"
              f"（人工真值：全部没进）")

    (out / "meta.jsonl").write_text(
        "\n".join(json.dumps(r, ensure_ascii=False) for r in rows),
        encoding="utf-8")
    n_tr = sum(1 for r in rows if r["split"] == "train")
    n_te = sum(1 for r in rows if r["split"] == "test")
    print(f"\n已导出 {out}/meta.jsonl：训练 {n_tr} 条，测试 {n_te} 条")
    print(f"切片在 {out}/patches/（每张是 2×{PATCH}×{PATCH} 灰度）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
