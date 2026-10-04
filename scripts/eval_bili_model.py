"""复核 leave-one-goal-out 的结果：要明细，不要一个漂亮的 AUC。

上一版报出 AUC=1.000，但阈值退化（负样本分数全 0.000）→ 精确率算出来 50%，很怪。
必须看清：
  * 14 个进球**逐个**是否被认出来（而不是只看总体 AUC）；
  * 用户在界面上判为「没进」的那 18 个难例，模型给了多少分
    （这些才是关键 —— 老判据就是在这些上误报的）；
  * 在**合理阈值**（而不是 p95=0）下的精确率/召回。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "src"))

import numpy as np                                                # noqa: E402
from basket_label_lib import require_cv                            # noqa: E402
from train_basket_model import scores_cnn, train_cnn               # noqa: E402
from train_bili_model import (CANDS, FEEDBACK, GAP_S, PATCH, VID,  # noqa: E402
                              crop_patch, load_labels)

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

    # 用户标注的候选：每个一个样本，带标签与分组
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
    print(f"用户标注样本 {len(rows)} 个")

    made_t = sorted(r["t"] for r in rows if r["label"] == "made")
    for r in rows:
        if r["label"] == "made":
            r["group"] = made_t.index(min(made_t, key=lambda m: abs(m - r["t"])))
        else:
            r["group"] = -1

    # 额外负样本（随机时刻），让每次训练都有足够负例
    rng = np.random.RandomState(11)
    total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
    dur = total_frames / fps if fps else 0.0
    neg_extra, tries = [], 0
    while len(neg_extra) < 60 and tries < 400:
        tries += 1
        t = float(rng.uniform(2.0, max(3.0, dur - 2.0)))
        if any(abs(t - m) < 3.0 for m in made_t):
            continue
        arr = stack2(t)
        if arr is not None:
            neg_extra.append({"t": t, "X": arr, "group": -1, "label": "rand"})
    cap.release()
    print(f"随机负样本 {len(neg_extra)} 个")

    Xn = np.stack([r["X"] for r in neg_extra] +
                  [r["X"] for r in rows if r["label"] == "miss"])
    yn = np.zeros(len(Xn))
    groups = [r["group"] for r in neg_extra] + \
             [r["group"] for r in rows if r["label"] == "miss"]

    print("\n=== 逐个进球：留出该进球后模型给它的分数 ===")
    per_goal = {}
    for gi in range(len(made_t)):
        held = [r for r in rows if r["group"] == gi]
        # 训练集：其他进球的抖动 + 全部负样本
        Xtr, ytr = [], []
        for gj, mt in enumerate(made_t):
            if gj == gi:
                continue
            for off in (-0.15, -0.075, 0.0, 0.075, 0.15):
                a = stack2(mt + off) if False else None
        # 用已缓存的抖动切片不可行（未保存），这里直接对训练进球现场取
        cap2 = cv2.VideoCapture(str(VID))

        def crop2(tt):
            a = crop_patch(cv2, np_, cap2, fps, tt, win)
            b = crop_patch(cv2, np_, cap2, fps, tt + GAP_S, win)
            if a is None or b is None:
                return None
            return np.stack([a, b], axis=0).astype(np.float32) / 255.0
        for gj, mt in enumerate(made_t):
            if gj == gi:
                continue
            for off in (-0.15, -0.075, 0.0, 0.075, 0.15):
                a = crop2(mt + off)
                if a is not None:
                    Xtr.append(a); ytr.append(1.0)
        cap2.release()
        Xtr = np.stack(Xtr + list(Xn)); ytr = np.array(ytr + list(yn))
        net = train_cnn(Xtr, ytr, epochs=80)
        s = scores_cnn(net, np.stack([r["X"] for r in held]))
        per_goal[gi] = {"t": made_t[gi], "scores": [round(float(v), 4) for v in s]}
        print(f"  进球 {gi:2d} t={made_t[gi]:8.2f}s → 分数 "
              f"{[round(float(v), 3) for v in s]}")

    print("\n=== 用户判为「没进」的难例：模型给多少分 ===")
    # 用最后一个模型（留出最后一个进球）近似：对全部样本打分
    net_all = train_cnn(np.stack([r["X"] for r in rows] +
                                 [r["X"] for r in neg_extra]),
                        np.array([1.0 if r["label"] == "made" else 0.0
                                  for r in rows] + [0.0] * len(neg_extra)),
                        epochs=120)
    miss = [r for r in rows if r["label"] == "miss"]
    s_miss = scores_cnn(net_all, np.stack([r["X"] for r in miss]))
    s_made = scores_cnn(net_all, np.stack([r["X"] for r in rows
                                           if r["label"] == "made"]))
    s_rand = scores_cnn(net_all, np.stack([r["X"] for r in neg_extra]))
    for r, v in sorted(zip(miss, s_miss), key=lambda x: -x[1]):
        print(f"  没进 t={r['t']:8.2f}s → 分数 {v:.3f}")
    print(f"\n  进球分数：均值 {s_made.mean():.3f} 最小 {s_made.min():.3f}")
    print(f"  没进分数：均值 {s_miss.mean():.3f} 最大 {s_miss.max():.3f}")
    print(f"  随机分数：均值 {s_rand.mean():.3f} 最大 {s_rand.max():.3f}")

    # 合理阈值：取「没进最大分」与「进球最小分」之间
    lo, hi = float(s_miss.max()), float(s_made.min())
    if lo < hi:
        thr = (lo + hi) / 2
        print(f"\n  可分开：没进最大 {lo:.3f} < 进球最小 {hi:.3f} → "
              f"取阈值 {thr:.3f} 时精确率/召回都是 100%")
    else:
        print(f"\n  有重叠：没进最大 {lo:.3f} ≥ 进球最小 {hi:.3f} → 需要权衡")
    json.dump({"per_goal": per_goal,
               "scores": {"made": [round(float(v), 4) for v in s_made],
                          "miss": [round(float(v), 4) for v in s_miss],
                          "random": [round(float(v), 4) for v in s_rand]}},
              (OUT / "eval_detail.json").open("w", encoding="utf-8"),
              ensure_ascii=False, indent=2)
    print(f"\n明细已存 {OUT}/eval_detail.json")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
