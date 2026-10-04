"""给用户标注的 32 个候选算**样本外**分数（分组交叉验证 + 多随机种子）。

为什么这样做才算数：
  * 用训练集自评会偏乐观（模型见过这些图）；
  * 之前 leave-one-goal-out 里负样本分数全是 0.000，看着完美但方差极大；
  * 这里：按进球事件分 5 折（同一次进球的样本永远在同一折），
    每个候选只在**没见过它的模型**上打分，然后算 AUC / 最优阈值下的精确率召回率。
  * 跑 3 个随机种子，报均值±标准差 —— 小数据集必须看方差，不能只看一次。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "src"))

import numpy as np                                                    # noqa: E402
from basket_label_lib import require_cv                                # noqa: E402
from train_basket_model import scores_cnn, train_cnn                   # noqa: E402
from train_bili_model import CANDS, GAP_S, VID, crop_patch, load_labels  # noqa: E402

OUT = ROOT / "out" / "basket_model_bili"


def main() -> int:
    cv2, np_ = require_cv()
    d = json.loads(CANDS.read_text(encoding="utf-8"))
    cands = d["candidates"]
    win = d["meta"].get("window") or cands[0]["win"]
    labels = load_labels(VID.name)

    cap = cv2.VideoCapture(str(VID))
    fps = cap.get(cv2.CAP_PROP_FPS) or 24.0

    def stack2(t):
        a = crop_patch(cv2, np_, cap, fps, t, win)
        b = crop_patch(cv2, np_, cap, fps, t + GAP_S, win)
        if a is None or b is None:
            return None
        return np.stack([a, b], axis=0).astype(np.float32) / 255.0

    rows = []
    for i, c in enumerate(cands):
        t = round(float(c["t0"]), 2)
        lab = None
        for tt, v in labels.items():
            if abs(tt - t) < 1.0:
                lab = v
                break
        if lab is None:
            continue
        arr = stack2(float(c["t0"]))
        if arr is None:
            continue
        rows.append({"idx": i, "t": float(c["t0"]), "label": lab, "X": arr})
    made_t = sorted(r["t"] for r in rows if r["label"] == "made")
    for r in rows:
        r["group"] = (made_t.index(min(made_t, key=lambda m: abs(m - r["t"])))
                      if r["label"] == "made" else -1)

    # 抖动正样本：同一进球事件的副本，**与它的进球同折**（不能泄漏）
    jit = []
    for mt in made_t:
        for off in (-0.12, -0.06, 0.06, 0.12):
            a = stack2(mt + off)
            if a is not None:
                jit.append({"X": a, "group": made_t.index(mt), "label": "made"})
    # 随机负样本：分到各折里（负样本不构成"事件"，随便分配）
    rng0 = np.random.RandomState(5)
    total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
    dur = total / fps if fps else 0.0
    rand = []
    tries = 0
    while len(rand) < 60 and tries < 400:
        tries += 1
        t = float(rng0.uniform(2.0, max(3.0, dur - 2.0)))
        if any(abs(t - m) < 3.0 for m in made_t):
            continue
        a = stack2(t)
        if a is not None:
            rand.append({"X": a, "group": -1, "label": "rand"})
    cap.release()
    print(f"标注候选 {len(rows)}（进球 {len(made_t)} 个事件），抖动 {len(jit)}，"
          f"随机负样本 {len(rand)}")

    pool = rows + [{"X": j["X"], "group": j["group"], "label": "made",
                    "t": None, "idx": None} for j in jit] + \
           [{"X": r["X"], "group": -1, "label": "rand", "t": None,
             "idx": None} for r in rand]
    Xall = np.stack([p["X"] for p in pool])
    yall = np.array([1.0 if p["label"] == "made" else 0.0 for p in pool])
    gall = np.array([p["group"] for p in pool])

    K = 5
    all_scores = {}
    for seed in (0, 1, 2):
        out_of_fold = np.full(len(pool), np.nan)
        # 把 14 个进球事件分 5 折；负样本（group=-1）随机均匀分到各折
        rng = np.random.RandomState(seed)
        perm = rng.permutation(len(made_t))
        fold_of_goal = {int(gi): int(k % K) for k, gi in enumerate(perm)}
        neg_idx = np.where(gall == -1)[0]
        neg_fold = rng.randint(0, K, size=len(neg_idx))
        fold_of = {}
        for i in neg_idx:
            fold_of[i] = int(neg_fold[list(neg_idx).index(i)])
        for i in np.where(gall >= 0)[0]:
            fold_of[i] = fold_of_goal[int(gall[i])]
        for k in range(K):
            te = np.array([fold_of.get(i, -1) == k for i in range(len(pool))])
            tr = ~te
            if te.sum() == 0 or yall[tr].sum() == 0:
                continue
            net = train_cnn(Xall[tr], yall[tr], epochs=120, seed=seed)
            out_of_fold[te] = scores_cnn(net, Xall[te])
        # 只取用户标注的那 32 个候选的样本外分数
        sc = out_of_fold[:len(rows)]
        all_scores[seed] = sc
        pos, neg = sc[[i for i, r in enumerate(rows) if r["label"] == "made"]], \
                   sc[[i for i, r in enumerate(rows) if r["label"] == "miss"]]
        auc = float(((pos[:, None] > neg[None, :]).sum() +
                     0.5 * (pos[:, None] == neg[None, :]).sum())
                    / (len(pos) * len(neg)))
        # 最优 F1 阈值
        cand_thr = np.unique(np.round(np.concatenate([pos, neg]), 3))
        best = (0.0, 0.5, 0.0)
        for th in cand_thr:
            tp = int((pos >= th).sum()); fp = int((neg >= th).sum())
            fn = len(pos) - tp
            prec = tp / (tp + fp) if tp + fp else 0.0
            rec = tp / (tp + fn) if tp + fn else 0.0
            f1 = 2 * prec * rec / (prec + rec) if prec + rec else 0.0
            if f1 > best[0]:
                best = (f1, float(th), prec)
        print(f"  seed={seed}: 样本外 AUC={auc:.3f}  最优 F1={best[0]:.2f} "
              f"(阈值 {best[1]:.3f}，精确率 {best[2]:.0%})  "
              f"进球分均值 {pos.mean():.3f} / 没进分均值 {neg.mean():.3f}")

    # 汇总：三个种子取平均分数，再看阈值
    mean_sc = np.mean([all_scores[s] for s in all_scores], axis=0)
    pos = np.array([mean_sc[i] for i, r in enumerate(rows) if r["label"] == "made"])
    neg = np.array([mean_sc[i] for i, r in enumerate(rows) if r["label"] == "miss"])
    print(f"\n三种子平均后：进球分 均值 {pos.mean():.3f}（最小 {pos.min():.3f}）；"
          f"没进分 均值 {neg.mean():.3f}（最大 {neg.max():.3f}）")
    # 逐条列出，便于人工核对哪些被判错
    print("\n逐条（样本外平均分，按分数降序）：")
    order = np.argsort(-mean_sc)
    n_wrong = 0
    for i in order:
        r = rows[int(i)]
        ok = (mean_sc[i] >= 0.5) == (r["label"] == "made")
        if not ok:
            n_wrong += 1
        print(f"  {'✓' if ok else '✗'} t={r['t']:8.2f}s  标注={r['label']:4s}  "
              f"分数 {mean_sc[i]:.3f}")
    print(f"\n以 0.5 为界限：{len(rows) - n_wrong}/{len(rows)} 条判对"
          f"（错 {n_wrong} 条）")

    json.dump({"per_seed": {str(k): [round(float(v), 4) for v in all_scores[k]]
                            for k in all_scores},
               "mean_scores": [round(float(v), 4) for v in mean_sc],
               "rows": [{"t": r["t"], "label": r["label"]} for r in rows]},
              (OUT / "oof_eval.json").open("w", encoding="utf-8"),
              ensure_ascii=False, indent=2)
    print(f"\n明细已存 {OUT}/oof_eval.json")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
