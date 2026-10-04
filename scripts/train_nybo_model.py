"""用用户素材训练 + **用原始 7 个候选当测试集**（诚实的验证）。

陷阱与对策：
  * 5 个正样本是**同一次进球**的相位抖动 → 不能随机划分训练/验证（会泄漏）。
    做法：随机留一个正样本出来做"相位验证"，其余 4 个进训练；
    真正的考验是**原始 7 个候选**（1 正 6 负，含 59.9s/127.5s 这两个难例）。
  * 负样本里混了"随机时刻"和"其他候选"，这两类用途不同：
    - 其他候选（cand_miss）是**难例**，最能检验模型；
    - 随机时刻是简单负例。
  * 报告里分开列这三组的结果，绝不混成一个好看的数字。
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "src"))

import numpy as np                                            # noqa: E402
from basket_label_lib import require_cv                        # noqa: E402
from export_basket_patches import export_patch                 # noqa: E402
from train_basket_model import (load_ds, scores_cnn, train_cnn,  # noqa: E402
                                numeric_features, scores_logreg,
                                train_logreg)

VID = ROOT / "data" / "nybo_3min.mp4"
MARKS = ROOT / "data" / "marks_nybo_3min.json"
CANDS = ROOT / "out" / "label_nybo" / "candidates.json"
GOAL_T = 173.42


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="用用户素材训练并诚实评估")
    ap.add_argument("--ds", default="out/basket_ds_nybo")
    ap.add_argument("--out", default="out/basket_model_nybo")
    ap.add_argument("--epochs", type=int, default=120)
    args = ap.parse_args(argv)

    ds = Path(args.ds)
    X, y, meta = load_ds(ds)
    print(f"数据集 {ds}：{len(y)} 条（正 {int(y.sum())} / 负 {int((y == 0).sum())}）")

    is_pos = y > 0.5
    pos_idx = np.where(is_pos)[0]
    # 留一个相位出来做验证
    hold = pos_idx[-1]
    tr = np.ones(len(y), dtype=bool)
    tr[hold] = False
    print(f"训练：正 {int(y[tr].sum())} / 负 {int((y[tr] == 0).sum())}；"
          f"留出的相位验证样本 t={meta[hold]['t']}")

    # ---------- 训练 ----------
    net = train_cnn(X[tr], y[tr], epochs=args.epochs)
    lr = train_logreg(numeric_features(meta)[0][tr], y[tr])

    # ---------- 组 1：留出的相位 ----------
    s_hold_cnn = scores_cnn(net, X[hold:hold + 1])[0]
    F_all, _ = numeric_features(meta)
    s_hold_lr = scores_logreg(lr, F_all[hold:hold + 1])[0]
    print(f"\n[组1] 留出的进球相位 t={meta[hold]['t']}s："
          f"CNN {s_hold_cnn:.3f}   逻辑回归 {s_hold_lr:.3f}")

    # ---------- 组 2：随机负样本 ----------
    rnd = np.array([m["src"] == "random" for m in meta])
    s_rnd_cnn = scores_cnn(net, X[rnd])
    s_rnd_lr = scores_logreg(lr, F_all[rnd])
    print(f"[组2] 随机负样本 {int(rnd.sum())} 个："
          f"CNN 均值 {s_rnd_cnn.mean():.3f} p95 {np.percentile(s_rnd_cnn, 95):.3f} | "
          f"逻辑回归 均值 {s_rnd_lr.mean():.3f} p95 {np.percentile(s_rnd_lr, 95):.3f}")

    # ---------- 组 3（真正的考验）：原始 7 个候选 ----------
    cv2, np_ = require_cv()
    mk = json.loads(MARKS.read_text(encoding="utf-8"))
    cx, cy, rx, ry = [float(v) for v in mk["hoop"]]
    cands = json.loads(CANDS.read_text(encoding="utf-8"))["candidates"]
    cap = cv2.VideoCapture(str(VID))
    fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
    print(f"\n[组3] 原始 {len(cands)} 个候选（真值：只有 t={GOAL_T}s 是进球）")
    print(f"     用的是**整段候选**（球块出现区间的中点），每张切片现场从视频裁")
    print(f"{'候选':>4} {'时刻':>8} {'真值':>5} {'CNN':>7} {'逻辑回归':>9}")
    buf = {}
    for i, c in enumerate(cands):
        t = float(c["t0"])
        key = f"cand{i}"
        arr, _xy, _f = export_patch(cv2, np_, cap, fps, t, cx, cy,
                                    Path(args.out) / "_tmp" / f"{key}.npy")
        if arr is None:
            print(f"{i:>4} {t:>8.2f}   取图失败")
            continue
        buf[key] = arr
    cap.release()

    if buf:
        keys = list(buf.keys())
        Xc = np.stack([buf[k] for k in keys])
        F = []
        for i, c in enumerate(cands):
            if f"cand{i}" not in buf:
                continue
            t = float(c["t0"])
            F.append([0.0] * len(numeric_features(meta)[1]))
        sc_cnn = scores_cnn(net, Xc)
        for j, (i, c) in enumerate([(i, c) for i, c in enumerate(cands)
                                    if f"cand{i}" in buf]):
            truth = "进球" if abs(float(c["t0"]) - GOAL_T) < 1.0 else "没进"
            print(f"{i:>4} {float(c['t0']):>8.2f} {truth:>5} "
                  f"{sc_cnn[j]:>7.3f} {'—':>9}")
        # 用留出相位的分数当阈值参考
        thr = float(np.percentile(s_rnd_cnn, 95))
        kept = [i for j, (i, c) in
                enumerate([(i, c) for i, c in enumerate(cands)
                           if f"cand{i}" in buf]) if sc_cnn[j] >= thr]
        print(f"\n      阈值（随机负样本 p95）= {thr:.3f} → 判出候选 {kept}")
        print(f"      真值进球是候选 5 → {'✅ 正确' if kept == [5] else '❌ 不对'}")

    Path(args.out).mkdir(parents=True, exist_ok=True)
    import torch
    torch.save(net.state_dict(), Path(args.out) / "cnn.pt")
    (Path(args.out) / "report.json").write_text(json.dumps({
        "trained_on": str(ds), "n_train": int(tr.sum()),
        "pos_in_train": int(y[tr].sum()), "random_neg": int(rnd.sum()),
        "holdout_phase_t": meta[hold]["t"],
        "holdout_score_cnn": round(float(s_hold_cnn), 4),
        "random_neg_p95_cnn": round(float(np.percentile(s_rnd_cnn, 95)), 4),
        "candidates": [{"t": float(c["t0"]),
                        "truth": bool(abs(float(c["t0"]) - GOAL_T) < 1.0)}
                       for c in cands],
        "note": ("正样本来自同一次进球的相位抖动（不是多次进球），"
                 "所以这里的数字只能说明'方向'，不能当泛化能力。"),
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n模型已存 {args.out}/cnn.pt（注意：正样本只有一次进球，别当泛化能力）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
