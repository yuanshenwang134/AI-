"""训练「进球判定」模型 —— 从手调阈值升级为**学出来的**判据。

背景（为什么要练它）：
  手写判据在 nybo_3min 上 precision = 0%（3 个判定全错，用户确认全是误报）。
  规则怎么调都救不了：那个机位篮筐贴画面顶边，"球从上方落下"没有判别信息。
  所以改成让模型从**有真值的数据**里学"进球在画面上长什么样"。

数据（scripts/export_basket_patches.py 导出）：
  训练：`basketball_match.mp4` + 比分牌真值 → 8 个正样本（真得分时刻）
        + 126 个负样本（其他时刻，篮筐附近有过球块运动）
  测试：`nybo_3min.mp4` + 用户人工标注（7 个候选全部没进）→ 全部负样本，
        用来检验"模型会不会在这段上也乱报进球"

做法（故意保守 —— 只有 8 个正样本）：
  1. **小 CNN**（~5 万参数）吃两帧相隔 0.12s 的篮筐切片（2×96×96）；
  2. 正样本极少 → 类别加权 + 轻量增广（翻转/平移）+ 权重衰减；
  3. 用**留一法**（每次留出一个正样本）估一个诚实的 AUC，而不是只看训练损失；
  4. 再跑一个**逻辑回归**（只用数值特征：网动/环带能量比、球块位置…）做对照 ——
     小数据上它常常比 CNN 稳，得让数据说话；
  5. 输出每个测试样本的分数，并检查"这一段全负的素材上模型是否也不乱报"。

用法：
    python scripts/export_basket_patches.py          # 先导数据
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


def load_ds(ds: Path):
    import numpy as np
    rows = [json.loads(x) for x in
            (ds / "meta.jsonl").read_text(encoding="utf-8").splitlines() if x.strip()]
    X, y, meta = [], [], []
    for r in rows:
        p = ds / r["patch"]
        if not p.exists():
            continue
        arr = np.load(p).astype(np.float32) / 255.0     # (2,96,96)
        X.append(arr)
        y.append(int(r["label"]))
        meta.append(r)
    return np.stack(X), np.array(y, dtype=np.float32), meta


def numeric_features(meta):
    """把 meta 里的数值特征拼成矩阵（逻辑回归用）。缺失填 0。"""
    import numpy as np
    keys = ["net_energy", "ring_energy", "net_ratio", "ball_relx", "ball_cy_rel",
            "ball_area", "blob_count"]
    X = []
    for r in meta:
        v = []
        for k in keys:
            x = r.get(k)
            v.append(0.0 if x is None else float(x))
        X.append(v)
    return np.array(X, dtype=np.float64), keys


def train_cnn(X, y, epochs=60, lr=3e-3, seed=0):
    """小 CNN：输入 2×96×96 → 一个 logit。正样本加权，带轻量增广。"""
    import numpy as np
    import torch
    import torch.nn as nn

    torch.manual_seed(seed)

    class Net(nn.Module):
        def __init__(self):
            super().__init__()
            self.f = nn.Sequential(
                nn.Conv2d(2, 12, 5, stride=2, padding=2), nn.ReLU(),   # 48
                nn.Conv2d(12, 24, 3, stride=2, padding=1), nn.ReLU(),  # 24
                nn.Conv2d(24, 32, 3, stride=2, padding=1), nn.ReLU(),  # 12
                nn.AdaptiveAvgPool2d(4), nn.Flatten(),
                nn.Linear(32 * 16, 32), nn.ReLU(), nn.Dropout(0.3),
                nn.Linear(32, 1))

        def forward(self, x):
            return self.f(x).squeeze(-1)

    net = Net()
    pos = float(y.sum())
    neg = float(len(y) - pos)
    w = torch.tensor([max(1.0, neg / max(1.0, pos))], dtype=torch.float32)
    opt = torch.optim.Adam(net.parameters(), lr=lr, weight_decay=1e-4)
    lossf = nn.BCEWithLogitsLoss(pos_weight=w)

    Xt = torch.tensor(X, dtype=torch.float32).unsqueeze(0) if X.ndim == 3 \
        else torch.tensor(X, dtype=torch.float32)
    yt = torch.tensor(y, dtype=torch.float32)
    n = len(Xt)
    rng = np.random.RandomState(seed)
    for ep in range(epochs):
        net.train()
        idx = rng.permutation(n)
        for i in range(0, n, 16):
            b = idx[i:i + 16]
            xb = Xt[b].clone()
            # 增广：随机水平翻转 + 上下左右平移 4px（切片小，位移不能大）
            for k in range(len(b)):
                if rng.rand() < 0.5:
                    xb[k] = torch.flip(xb[k], dims=[2])
                dx, dy = rng.randint(-4, 5), rng.randint(-4, 5)
                xb[k] = torch.roll(xb[k], shifts=(dy, dx), dims=(1, 2))
            opt.zero_grad()
            out = net(xb)
            loss = lossf(out, yt[b])
            loss.backward()
            opt.step()
    return net


def scores_cnn(net, X):
    import numpy as np
    import torch
    net.eval()
    with torch.no_grad():
        Xt = torch.tensor(X, dtype=torch.float32)
        if Xt.ndim == 3:
            Xt = Xt.unsqueeze(0)
        return torch.sigmoid(net(Xt)).numpy()


def train_logreg(F, y, l2=1.0):
    """手写逻辑回归（环境里没有 sklearn，用 numpy 实现）。

    小数据 + 数值特征常常比 CNN 稳，所以必须做这个对照。
    特征标准化后做梯度下降 + L2。
    """
    import numpy as np
    mu, sd = F.mean(0), F.std(0) + 1e-6
    Z = (F - mu) / sd
    Z = np.hstack([Z, np.ones((len(Z), 1))])
    w = np.zeros(Z.shape[1])
    pos = max(1.0, y.sum())
    neg = max(1.0, len(y) - y.sum())
    cw = np.where(y > 0.5, neg / pos, 1.0)
    for _ in range(4000):
        z = Z @ w
        p = 1.0 / (1.0 + np.exp(-np.clip(z, -30, 30)))
        g = Z.T @ (cw * (p - y)) / len(y) + l2 * np.r_[w[:-1], 0.0] / len(y)
        w -= 0.5 * g
    return {"w": w, "mu": mu, "sd": sd}


def scores_logreg(model, F):
    import numpy as np
    Z = (F - model["mu"]) / model["sd"]
    Z = np.hstack([Z, np.ones((len(Z), 1))])
    z = Z @ model["w"]
    return 1.0 / (1.0 + np.exp(-np.clip(z, -30, 30)))


def loo_auc(make_scores, X, y, tag=""):
    """留一法估 AUC（只有 8 个正样本，必须这样估才诚实）。"""
    import numpy as np
    scores = np.zeros(len(y))
    for i in range(len(y)):
        tr = np.ones(len(y), dtype=bool)
        tr[i] = False
        if y[tr].sum() == 0:
            continue
        sc = make_scores(X[tr], y[tr], X[i:i + 1])
        scores[i] = sc[0]
    # AUC（正/负对比较）
    pos = scores[y > 0.5]
    neg = scores[y < 0.5]
    if len(pos) == 0 or len(neg) == 0:
        return float("nan"), scores
    wins = (pos[:, None] > neg[None, :]).sum() + \
           0.5 * (pos[:, None] == neg[None, :]).sum()
    return float(wins) / (len(pos) * len(neg)), scores


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="训练进球判定模型")
    ap.add_argument("--ds", default="out/basket_ds")
    ap.add_argument("--out", default="out/basket_model")
    ap.add_argument("--epochs", type=int, default=60)
    ap.add_argument("--device", default="0")
    args = ap.parse_args(argv)

    import numpy as np

    ds = Path(args.ds)
    X, y, meta = load_ds(ds)
    tr = np.array([m["split"] == "train" for m in meta])
    te = ~tr
    print(f"数据集：训练 {int(tr.sum())}（正 {int(y[tr].sum())} / 负 "
          f"{int((y[tr] == 0).sum())}），测试 {int(te.sum())}"
          f"（正 {int(y[te].sum())} / 负 {int((y[te] == 0).sum())}）")
    if tr.sum() == 0 or y[tr].sum() == 0:
        print("训练集里没有正样本，无法训练。先跑 export_basket_patches.py")
        return 2

    F, fkeys = numeric_features(meta)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    # ---------- 逻辑回归（数值特征）----------
    lr_model = train_logreg(F[tr], y[tr])
    s_lr_all = scores_logreg(lr_model, F)
    pos, neg = s_lr_all[tr & (y > 0.5)], s_lr_all[tr & (y < 0.5)]
    print(f"\n[逻辑回归] 训练集：正样本分数 均值 {pos.mean():.3f} "
          f"范围 {pos.min():.3f}~{pos.max():.3f}；负样本 均值 {neg.mean():.3f} "
          f"p95 {np.percentile(neg, 95):.3f}")
    print(f"           测试集 nybo 7 个候选（真值全为没进）："
          f"{', '.join('%.3f' % s for s in s_lr_all[te])}")

    # ---------- 小 CNN ----------
    net = train_cnn(X[tr], y[tr], epochs=args.epochs)
    s_cnn_all = scores_cnn(net, X)
    pos_c, neg_c = s_cnn_all[tr & (y > 0.5)], s_cnn_all[tr & (y < 0.5)]
    print(f"\n[小 CNN] 训练集：正样本分数 均值 {pos_c.mean():.3f} "
          f"范围 {pos_c.min():.3f}~{pos_c.max():.3f}；负样本 均值 {neg_c.mean():.3f} "
          f"p95 {np.percentile(neg_c, 95):.3f}")
    print(f"         测试集 nybo 7 个候选（真值全为没进）："
          f"{', '.join('%.3f' % s for s in s_cnn_all[te])}")

    # ---------- 留一法 AUC（诚实估计）----------
    # 注意：切片特征（CNN 用）与数值特征（逻辑回归用）索引不同，
    # 所以两种模型各自用自己的矩阵做留一。
    idx_tr = np.where(tr)[0]
    Fr, yr = F[idx_tr], y[idx_tr]
    auc_lr, _ = loo_auc(lambda Xa, ya, Xq: scores_logreg(
        train_logreg(Xa, ya), Xq), Fr, yr)
    print(f"\n留一法 AUC（逻辑回归，数值特征）：{auc_lr:.2f}"
          if not math.isnan(auc_lr) else "\n留一法 AUC 无法计算（样本不足）")

    def mk_cnn(Xa, ya, Xq):
        n2 = train_cnn(Xa, ya, epochs=max(20, args.epochs // 2))
        return scores_cnn(n2, Xq)
    auc_cnn, _ = loo_auc(mk_cnn, X[tr], y[tr])
    print(f"留一法 AUC（小 CNN）：{auc_cnn:.2f}"
          if not math.isnan(auc_cnn) else "留一法 AUC 无法计算（样本不足）")

    # ---------- 阈值选取：在"敢判就尽量准"的取向下 ----------
    # 用训练集负样本的 p95 当阈值：误报率压到 ~5%，代价是正样本召回会掉。
    thr_lr = float(np.percentile(neg, 95))
    thr_cnn = float(np.percentile(neg_c, 95))
    print(f"\n建议阈值（取训练负样本 p95，≈5% 误报）："
          f"逻辑回归 {thr_lr:.3f}，小 CNN {thr_cnn:.3f}")
    kept_lr = int((s_lr_all[tr & (y > 0.5)] >= thr_lr).sum())
    kept_cnn = int((s_cnn_all[tr & (y > 0.5)] >= thr_cnn).sum())
    print(f"  该阈值下训练集正样本召回：逻辑回归 {kept_lr}/{int(y[tr].sum())}，"
          f"小 CNN {kept_cnn}/{int(y[tr].sum())}")
    print(f"  nybo（真值全负）被判成进球的个数：逻辑回归 "
          f"{int((s_lr_all[te] >= thr_lr).sum())}，小 CNN "
          f"{int((s_cnn_all[te] >= thr_cnn).sum())}")

    # ---------- 存盘 ----------
    import torch
    torch.save(net.state_dict(), out / "cnn.pt")
    (out / "logreg.json").write_text(json.dumps({
        "w": lr_model["w"].tolist(), "mu": lr_model["mu"].tolist(),
        "sd": lr_model["sd"].tolist(), "features": fkeys,
        "threshold": thr_lr}, ensure_ascii=False, indent=2), encoding="utf-8")
    (out / "report.json").write_text(json.dumps({
        "train": {"n": int(tr.sum()), "pos": int(y[tr].sum())},
        "test": {"n": int(te.sum()), "pos": int(y[te].sum())},
        "loo_auc": {"logreg": auc_lr, "cnn": auc_cnn},
        "threshold": {"logreg": thr_lr, "cnn": thr_cnn},
        "test_scores": {"logreg": [round(float(v), 4) for v in s_lr_all[te]],
                        "cnn": [round(float(v), 4) for v in s_cnn_all[te]]},
        "test_meta": [{"t": m["t"], "label": m["label"]} for m in meta if m["split"] == "test"],
        "note": ("正样本只有 8 个（比分牌真值），规模很小：这里的 AUC 是留一法估计，"
                 "别当成生产级指标。链路的意义是——判据从此**可以学**，"
                 "用户每在界面上确认一球（/api/feedback），样本就多一个。")
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n模型已存：{out}/cnn.pt、{out}/logreg.json、{out}/report.json")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
