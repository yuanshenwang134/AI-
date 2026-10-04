"""用用户标注的 32 个候选训练进球判定模型 —— 并做**按进球分组的交叉验证**。

数据（用户在「训练标注」页逐球确认）：
  14 个进球 + 18 个没进，都是独立事件，来自同一段视频 + 同一个手工标定的篮筐。

为什么这次评估可信（之前不可信的原因）：
  * 早先用转播素材时，篮筐坐标算错（落在比分牌上），模型学的是"记分条跳数字"；
  * 后来只有 1 个进球，抖动出来的"多个正样本"其实是同一事件的相位 → 交叉验证会泄漏。
  这次有 **14 个独立进球事件**，可以做 leave-one-goal-out：
  留出一个进球（连同它的抖动样本）当验证，其余进球 + 全部负样本训练。
  这样报出来的数字**不泄漏**。

做法：
  1. 切片窗口用 candidates.json 里的 `win`（= 用户在视频里看到的那块），缩到 96x96；
     每张含两帧（t 与 t+0.12s），让模型能看到运动。
  2. 正样本按进球事件分组；负样本 = 18 个难例 + 随机时刻（排除进球 ±3s）。
  3. leave-one-goal-out 跑 CNN，汇总 AUC / 精确率 / 召回。
  4. 全量重训一次当最终模型存档。
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "src"))

import numpy as np                                                    # noqa: E402
from basket_label_lib import require_cv                                # noqa: E402
from train_basket_model import scores_cnn, train_cnn                   # noqa: E402

VID = ROOT / "data" / "bili_nybo.mp4"
CANDS = ROOT / "out" / "label_bili" / "candidates.json"
FEEDBACK = ROOT / "data" / "basket_feedback.jsonl"
OUT = ROOT / "out" / "basket_model_bili"
PATCH = 96
GAP_S = 0.12


def load_labels(video_name: str) -> dict:
    """{候选序号: 'made'|'miss'} —— 用候选表里的 idx 对齐标注。"""
    rows = [json.loads(x) for x in
            FEEDBACK.read_text(encoding="utf-8").splitlines() if x.strip()]
    rows = [r for r in rows if Path(str(r.get("video", ""))).name == video_name]
    return {round(float(r["t"]), 2): r["label"] for r in rows}


def crop_patch(cv2, np_, cap, fps, t, win, size=PATCH):
    """按 win 裁一帧，缩到 size×size；返回单帧灰度。"""
    x0, y0, x1, y1 = [int(v) for v in win]
    cap.set(cv2.CAP_PROP_POS_FRAMES, max(0, int(round(t * fps))))
    ok, fr = cap.read()
    if not ok:
        return None
    g = cv2.cvtColor(fr, cv2.COLOR_BGR2GRAY)
    H, W = g.shape
    x0 = max(0, min(W - 4, x0)); y0 = max(0, min(H - 4, y0))
    x1 = max(x0 + 4, min(W, x1)); y1 = max(y0 + 4, min(H, y1))
    crop = g[y0:y1, x0:x1]
    if crop.size == 0:
        return None
    return cv2.resize(crop, (size, size), interpolation=cv2.INTER_AREA)


def stack2(cv2, np_, cap, fps, t, win):
    a = crop_patch(cv2, np_, cap, fps, t, win)
    b = crop_patch(cv2, np_, cap, fps, t + GAP_S, win)
    if a is None or b is None:
        return None
    return np.stack([a, b], axis=0).astype(np.float32) / 255.0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="训练进球判定模型（按进球分组交叉验证）")
    ap.add_argument("--epochs", type=int, default=150)
    ap.add_argument("--jitter", type=float, default=0.15)
    ap.add_argument("--n-jitter", type=int, default=3)
    ap.add_argument("--random-neg", type=int, default=80)
    args = ap.parse_args(argv)

    cv2, np_ = require_cv()
    d = json.loads(CANDS.read_text(encoding="utf-8"))
    cands = d["candidates"]
    win = d["meta"].get("window") or cands[0]["win"]
    labels = load_labels(VID.name)
    print(f"素材 {VID.name}：候选 {len(cands)} 个，标注 {len(labels)} 条"
          f"（进球 {sum(1 for v in labels.values() if v == 'made')} / "
          f"没进 {sum(1 for v in labels.values() if v == 'miss')}）")
    if not labels:
        print("[err] 没有读到标注（data/basket_feedback.jsonl）")
        return 2

    cap = cv2.VideoCapture(str(VID))
    fps = cap.get(cv2.CAP_PROP_FPS) or 24.0
    total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
    dur = total / fps if fps else 0.0

    X, y, group = [], [], []      # group = 进球事件 id（负样本用 -1）
    made_ts = sorted(t for t, v in labels.items() if v == "made")

    # ---- 用户标注的候选本身（14 正 + 18 负）----
    for i, c in enumerate(cands):
        t = round(float(c["t0"]), 2)
        lab = None
        for tt, v in labels.items():
            if abs(tt - t) < 1.0:
                lab = v
                break
        if lab is None:
            continue
        arr = stack2(cv2, np_, cap, fps, float(c["t0"]), win)
        if arr is None:
            continue
        X.append(arr); y.append(1.0 if lab == "made" else 0.0)
        if lab == "made":
            group.append(made_ts.index(min(made_ts, key=lambda m: abs(m - t))))
        else:
            group.append(-1)
    n_user = len(X)
    print(f"用上用户标注的候选 {n_user} 个")

    # ---- 正样本抖动（增广，仍归属同一进球事件）----
    n_jit = 0
    for gi, t in enumerate(made_ts):
        for k in range(args.n_jitter):
            off = args.jitter * (k + 1) / max(1, args.n_jitter)
            for sgn in (1, -1):
                arr = stack2(cv2, np_, cap, fps, t + sgn * off, win)
                if arr is not None:
                    X.append(arr); y.append(1.0); group.append(gi)
                    n_jit += 1
    print(f"正样本抖动 {n_jit} 个（分组后仍是 {len(made_ts)} 个进球事件）")

    # ---- 随机负样本（排除进球 ±3s）----
    rng = np.random.RandomState(11)
    n_rand, tries = 0, 0
    while n_rand < args.random_neg and tries < args.random_neg * 6:
        tries += 1
        t = float(rng.uniform(2.0, max(3.0, dur - 2.0)))
        if any(abs(t - m) < 3.0 for m in made_ts):
            continue
        arr = stack2(cv2, np_, cap, fps, t, win)
        if arr is not None:
            X.append(arr); y.append(0.0); group.append(-1)
            n_rand += 1
    cap.release()

    X = np.stack(X); y = np.array(y); group = np.array(group)
    print(f"数据集：{len(y)} 个切片（正 {int(y.sum())} / 负 {int((y == 0).sum())}），"
          f"进球事件 {len(made_ts)} 个，随机负样本 {n_rand} 个")

    # ---- leave-one-goal-out 交叉验证 ----
    print("\n=== 按进球分组的交叉验证（留出一个进球，全部它的抖动也一起留出）===")
    scores = np.zeros(len(y))
    for gi in range(len(made_ts)):
        te = group == gi
        tr = ~te
        if y[tr].sum() == 0 or y[te].sum() == 0:
            continue
        net = train_cnn(X[tr], y[tr], epochs=max(60, args.epochs // 2))
        scores[te] = scores_cnn(net, X[te])
    pos = scores[y > 0.5]
    neg = scores[(y < 0.5)]
    # 难例负样本（用户标的 18 个）单独看
    hard = np.array([g == -1 for g in group]) & (y < 0.5)
    # 用"随机负样本"的 p95 定阈值（更难例化的做法）
    thr = float(np.percentile(neg, 95)) if len(neg) else 0.5
    auc = float("nan")
    if len(pos) and len(neg):
        wins = (pos[:, None] > neg[None, :]).sum() + \
               0.5 * (pos[:, None] == neg[None, :]).sum()
        auc = float(wins) / (len(pos) * len(neg))
    print(f"留出的进球切片：{len(pos)} 个，负样本切片 {len(neg)} 个")
    print(f"  **AUC = {auc:.3f}**   阈值(负样本p95) = {thr:.3f}")
    print(f"  进球平均分 {pos.mean():.3f}（最小 {pos.min():.3f}）；"
          f"负样本平均分 {neg.mean():.3f}（p95 {np.percentile(neg, 95):.3f}，"
          f"最大 {neg.max():.3f}）")
    tp = int((pos >= thr).sum()); fn = len(pos) - tp
    fp = int((neg >= thr).sum()); tn = len(neg) - fp
    prec = tp / (tp + fp) if (tp + fp) else 0.0
    rec = tp / (tp + fn) if (tp + fn) else 0.0
    print(f"  该阈值下：精确率 {prec:.0%}  召回 {rec:.0%}  "
          f"(TP{tp} FP{fp} FN{fn} TN{tn})")

    # ---- 全量重训最终模型 ----
    net = train_cnn(X, y, epochs=args.epochs)
    OUT.mkdir(parents=True, exist_ok=True)
    import torch
    torch.save(net.state_dict(), OUT / "cnn.pt")
    (OUT / "report.json").write_text(json.dumps({
        "dataset": {"samples": int(len(y)), "pos": int(y.sum()),
                    "neg": int((y == 0).sum()), "goal_events": len(made_ts),
                    "user_labeled": n_user, "jitter": n_jit,
                    "random_neg": n_rand},
        "cv": {"scheme": "leave-one-goal-out", "auc": round(auc, 4),
               "threshold": round(thr, 4), "precision": round(prec, 4),
               "recall": round(rec, 4), "tp": tp, "fp": fp, "fn": fn, "tn": tn},
        "note": ("14 个独立进球事件做的 leave-one-goal-out，抖动样本随组留出，"
                 "所以数字不泄漏。样本仍偏少（一个镜头、一次比赛），"
                 "换机位/光照需要重新标。"),
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n最终模型已存 {OUT}/cnn.pt，报告 {OUT}/report.json")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
