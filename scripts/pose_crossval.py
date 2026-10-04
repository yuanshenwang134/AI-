"""③ 第 1、2 步：修掉坏特征 + 跨素材复核（防止只在 bili 上过拟合）。

问题 1（坏特征）：上一版 `near_mean`（筐下人数占比）**恒为 0**。
  原因：判定用的是绝对像素框 `abs(fy-cy)<220`，而 bili 的篮筐在画面顶部（cy=30），
  球员都在它下方几百像素处 → 全部落框外。绝对像素阈值本来就不该用。

问题 2（缺复核）：AUC 0.884 只来自单段素材的留一法。
  本次做**跨素材**检验：在 bili（32 个标注）上训练，拿到 sample_dairy.mov 上测
  —— 那段有 2 个"确认过的真进球"（17.1s / 26.0s，逐帧核对过）+ 一批随机负窗口。
  如果 2 个真进球在负窗口里排到最前面，说明特征有跨素材的判别力。

新特征（都用**相对尺度**，不写死像素）：
  n_mean / n_max     人数
  jump_p90 / jump_max  脚点竖直速度（起跳）
  min_dist           最近球员到篮筐的距离 / 画面宽
  n_near             筐附近（0.35×画面宽内）人数均值
  approach           窗口内"最近距离"的下降量（有人朝筐靠近）
  swing              手腕竖直摆幅（举手/投篮）
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import cv2                                                   # noqa: E402
import numpy as np                                           # noqa: E402
from ultralytics import YOLO                                 # noqa: E402

POSE_W = ROOT / "yolo11n-pose.pt"
WIN_S, STRIDE = 1.5, 3
FEATS = ["n_mean", "n_max", "jump_p90", "jump_max", "min_dist",
         "n_near", "approach", "swing"]


def feats_window(model, cap, fps, t_center, hoop):
    cx, cy = float(hoop[0]), float(hoop[1])
    W = float(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    f0 = int(max(0.0, t_center - WIN_S) * fps)
    f1 = int((t_center + WIN_S) * fps)
    people, jumps, dists, nears, swings = [], [], [], [], []
    prev = {}
    for f in range(f0, f1 + 1, STRIDE):
        cap.set(cv2.CAP_PROP_POS_FRAMES, f)
        ok, fr = cap.read()
        if not ok:
            continue
        r = model.predict(fr, conf=0.3, verbose=False, device="cpu")[0]
        b = r.boxes
        n = 0 if b is None else len(b)
        people.append(n)
        if b is not None and len(b):
            xyxy = b.xyxy.cpu().numpy()
            feet = [((x1 + x2) / 2.0, float(y2)) for x1, y1, x2, y2 in xyxy]
            for (fx, fy) in feet:
                k = int(fx // 40)
                if k in prev:
                    jumps.append(prev[k] - fy)      # 脚上移为正 = 起跳
                prev[k] = fy
            # 相对尺度的"离筐远近"（不写死像素）
            ds = [float(np.hypot(fx - cx, fy - cy)) / W for (fx, fy) in feet]
            dists.append(min(ds))
            nears.append(sum(1 for d in ds if d < 0.35))
        if getattr(r, "keypoints", None) is not None and r.keypoints is not None:
            xy = r.keypoints.xy.cpu().numpy()
            if len(xy):
                wy = xy[:, :, 1]
                swings.append(float(np.nanmax(wy) - np.nanmin(wy)) / W)
    if len(people) < 5:
        return None
    j = np.array(jumps) if jumps else np.array([0.0])
    d = np.array(dists) if dists else np.array([1.0])
    return {
        "n_mean": round(float(np.mean(people)), 2),
        "n_max": int(np.max(people)),
        "jump_p90": round(float(np.percentile(j, 90)), 2),
        "jump_max": round(float(np.max(j)), 2),
        "min_dist": round(float(np.min(d)), 4),
        "n_near": round(float(np.mean(nears)) if nears else 0.0, 2),
        "approach": round(float(d[0] - np.min(d)) if len(d) else 0.0, 4),
        "swing": round(float(np.mean(swings)) if swings else 0.0, 4),
    }


def collect(model, video, times_labels, hoop_of):
    cap = cv2.VideoCapture(str(video))
    fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
    rows = []
    for t, lab in times_labels:
        hoop = hoop_of(t)
        f = feats_window(model, cap, fps, float(t), hoop)
        if f:
            rows.append({"t": float(t), "label": lab, **f})
    cap.release()
    return rows


def logistic_fit(X, y, l2=1.0, iters=4000):
    mu, sd = X.mean(0), X.std(0) + 1e-6
    Z = np.hstack([(X - mu) / sd, np.ones((len(X), 1))])
    w = np.zeros(Z.shape[1])
    pos = max(1.0, y.sum()); neg = max(1.0, (1 - y).sum())
    cw = np.where(y > 0.5, neg / pos, 1.0)
    for _ in range(iters):
        p = 1 / (1 + np.exp(-np.clip(Z @ w, -30, 30)))
        w -= 0.5 * (Z.T @ (cw * (p - y)) / len(y) + l2 * np.r_[w[:-1], 0.0] / len(y))
    return {"w": w, "mu": mu, "sd": sd}


def logistic_score(m, X):
    Z = np.hstack([(X - m["mu"]) / m["sd"], np.ones((len(X), 1))])
    return 1 / (1 + np.exp(-np.clip(Z @ m["w"], -30, 30)))


def loo(X, y):
    sc = np.zeros(len(y))
    for i in range(len(y)):
        tr = np.ones(len(y), bool); tr[i] = False
        if y[tr].sum() == 0 or y[tr].sum() == tr.sum():
            sc[i] = 0.5; continue
        sc[i] = logistic_score(logistic_fit(X[tr], y[tr]), X[i:i + 1])[0]
    return sc


def auc(pos, neg):
    if not len(pos) or not len(neg):
        return float("nan")
    return float(((pos[:, None] > neg[None, :]).sum()
                  + 0.5 * (pos[:, None] == neg[None, :]).sum())
                 / (len(pos) * len(neg)))


def main() -> int:
    if not POSE_W.exists():
        print("[err] 缺 yolo11n-pose.pt")
        return 2
    model = YOLO(str(POSE_W))

    # ---- 素材 A：bili（用户标注的 32 个候选）----
    d = json.loads((ROOT / "out/label_bili/candidates.json").read_text(encoding="utf-8"))
    cands = d["candidates"]
    fb = [json.loads(x) for x in
          (ROOT / "data/basket_feedback.jsonl").read_text(encoding="utf-8").splitlines()
          if x.strip()]
    fb = [r for r in fb if "bili" in str(r.get("video", ""))]
    tl_a, seen = [], set()
    for c in cands:
        lab = next((r["label"] for r in fb
                    if abs(float(r["t"]) - float(c["t0"])) < 1.0), None)
        if lab and c["t0"] not in seen:
            seen.add(c["t0"]); tl_a.append((c["t0"], lab))
    hoop_a = {c["t0"]: c["hoop"] for c in cands}

    print(f"[A] bili_nybo：{len(tl_a)} 个标注窗口")
    rowsA = collect(model, ROOT / "data/bili_nybo.mp4", tl_a, lambda t: hoop_a[t])

    # ---- 素材 B：sample_dairy（2 个已确认进球 + 随机负窗口）----
    hoop_b = [880.0, 205.0, 60.0, 20.0]     # sample_dairy 的篮筐（估）
    # 先用一帧自动定位篮筐（用 rim 权重）
    try:
        rim = YOLO(str(ROOT / "runs/detect/rim/weights/best.pt"))
        cap = cv2.VideoCapture(str(ROOT / "data/sample_dairy.mov"))
        cap.set(cv2.CAP_PROP_POS_FRAMES, int(17.1 * 30)); ok, fr = cap.read()
        cap.release()
        if ok:
            rr = rim.predict(fr, conf=0.2, verbose=False, device="cpu")[0]
            if rr.boxes is not None and len(rr.boxes):
                x1, y1, x2, y2 = [float(v) for v in rr.boxes.xyxy[0]]
                hoop_b = [(x1 + x2) / 2, (y1 + y2) / 2, (x2 - x1) / 2, (y2 - y1) / 2]
        print(f"[B] sample_dairy 篮筐（rim 权重定位）：{[round(v,1) for v in hoop_b]}")
    except Exception as e:  # noqa: BLE001
        print(f"[B] 篮筐定位失败，用估计值：{e}")
    pos_b = [(17.1, "made"), (26.0, "made")]
    neg_b = [(t / 2.0, "miss") for t in range(2, 56, 3)
             if abs(t / 2.0 - 17.1) > 2.0 and abs(t / 2.0 - 26.0) > 2.0]
    print(f"[B] sample_dairy：{len(pos_b)} 个进球 + {len(neg_b)} 个负窗口")
    rowsB = collect(model, ROOT / "data/sample_dairy.mov", pos_b + neg_b,
                    lambda t: hoop_b)

    if not rowsA or not rowsB:
        print("[err] 特征提取不完整")
        return 1

    # ---- 1) bili 上的留一法（修掉坏特征后的水平）----
    XA = np.array([[r[k] for k in FEATS] for r in rowsA], float)
    yA = np.array([1.0 if r["label"] == "made" else 0.0 for r in rowsA])
    scA = loo(XA, yA)
    pA, nA = scA[yA > 0.5], scA[yA < 0.5]
    print(f"\n=== [1] bili 留一法（坏特征已修）AUC {auc(pA, nA):.3f} ===")
    best = (0.0, 0.5, 0.0, 0.0)
    for th in np.unique(np.round(scA, 3)):
        tp = int((pA >= th).sum()); fp = int((nA >= th).sum()); fn = len(pA) - tp
        p = tp / (tp + fp) if tp + fp else 0.0
        rc = tp / (tp + fn) if tp + fn else 0.0
        f1 = 2 * p * rc / (p + rc) if p + rc else 0.0
        if f1 > best[0]:
            best = (f1, float(th), p, rc)
    print(f"    最优 F1 {best[0]:.2f}（阈值 {best[1]:.2f}，精确率 {best[2]:.0%}，"
          f"召回 {best[3]:.0%}）")

    # ---- 2) 跨素材：bili 训练 → sample_dairy 测试 ----
    m = logistic_fit(XA, yA)
    XB = np.array([[r[k] for k in FEATS] for r in rowsB], float)
    scB = logistic_score(m, XB)
    yB = np.array([1.0 if r["label"] == "made" else 0.0 for r in rowsB])
    pB, nB = scB[yB > 0.5], scB[yB < 0.5]
    print(f"\n=== [2] 跨素材（bili 训练 → sample_dairy 测试）AUC {auc(pB, nB):.3f} ===")
    print("    两个真进球的分数：" +
          "、".join(f"t={r['t']}s → {s:.3f}"
                    for r, s in zip(rowsB, scB) if r["label"] == "made"))
    rank = sorted(range(len(scB)), key=lambda i: -scB[i])
    pos_rank = [i for i in rank if yB[i] > 0.5]
    print(f"    它们在 {len(scB)} 个窗口里排第 "
          f"{[rank.index(i) + 1 for i in pos_rank]} 名")
    print(f"    负窗口分数范围 {nB.min():.3f}~{nB.max():.3f}，中位 {np.median(nB):.3f}")

    (ROOT / "out" / "pose_crossval.json").write_text(json.dumps(
        {"bili_loo": {"auc": auc(pA, nA), "f1": best[0], "precision": best[2],
                      "recall": best[3], "n": len(rowsA)},
         "cross": {"auc": auc(pB, nB),
                   "pos_scores": [round(float(s), 3) for s, r in zip(scB, rowsB)
                                  if r["label"] == "made"],
                   "neg_range": [round(float(nB.min()), 3),
                                 round(float(nB.max()), 3)],
                   "ranks": [rank.index(i) + 1 for i in pos_rank],
                   "n_neg": int(len(nB))},
         "rowsA": rowsA, "rowsB": rowsB}, ensure_ascii=False, indent=2),
        encoding="utf-8")
    print("\n明细已存 out/pose_crossval.json")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
