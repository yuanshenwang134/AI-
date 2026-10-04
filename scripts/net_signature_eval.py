"""不追球的自动判定：用**篮网脉冲响应**识别进球（稳像 + 波形特征 + 留一法验证）。

为什么换这个思路：
  球只有 7~15px 且被运动模糊糊掉，八种"追球"的方法全部失败（实测）。
  但球穿网时**网会被带动**，这是发生在**固定位置**的强信号，不需要看清球。
  上一次我只用了"网动能量峰值"→ F1 只有 0.65、精确率 48%（因为球员从筐下走过
  也会让这个区域动）。这次改提取**波形特征**：
    真实进球 = 急起（1~2 帧内到峰）→ 振荡（多个局部极大）→ 衰减（0.5~1.5s）
    球员走过 = 缓慢上升、无振荡、持续时间长
  再用用户标注的 32 个候选（14 正 / 18 负）做**留一法**验证，给出精确率/召回。

同时解决镜头平移：用**相位相关**逐帧估计平移量并补偿（比 ORB 快 100 倍，
平移镜头足够用），把网区"钉住"后再算运动。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import cv2                                                    # noqa: E402
import numpy as np                                            # noqa: E402

VID = ROOT / "data" / "bili_nybo.mp4"
CANDS = ROOT / "out" / "label_bili" / "candidates.json"
FEEDBACK = ROOT / "data" / "basket_feedback.jsonl"
PAD_BEFORE, PAD_AFTER = 1.3, 0.6
SCALE = 0.5          # 降采样加速


def regions(hoop, shape):
    """网区（篮圈下方漏斗）与对照环带（圈外同高）。"""
    cx, cy, rx, ry = hoop
    H, W = shape
    yy, xx = np.mgrid[0:H, 0:W]
    dx = (xx - cx) / max(1.0, rx)
    dy = (yy - cy) / max(1.0, ry)
    r = np.sqrt(dx ** 2 + dy ** 2)
    net = (r <= 1.05) & (yy >= cy - 0.2 * ry) & (yy <= cy + 3.0 * ry)
    ring = (r > 1.3) & (r <= 2.2) & (yy <= cy + 3.0 * ry)
    return net, ring


def window_features(cap, fps, t_center, hoop):
    """取候选窗口，稳像后提取网动波形特征。"""
    f0 = int(max(0.0, t_center - PAD_BEFORE) * fps)
    f1 = int((t_center + PAD_AFTER) * fps)
    cx, cy, rx, ry = hoop
    x0 = int(max(0, cx - 2.5 * rx)); x1 = int(cx + 2.5 * rx)
    y0 = int(max(0, cy - 1.5 * ry)); y1 = int(cy + 4.0 * ry)
    # 掩膜必须按**降采样后**的尺寸来算（之前按原尺寸算、帧却缩了 0.5 倍 → 越界）
    ch, cw = int((y1 - y0) * SCALE), int((x1 - x0) * SCALE)
    ner, rir = regions(((cx - x0) * SCALE, (cy - y0) * SCALE,
                        rx * SCALE, ry * SCALE), (ch, cw))
    if ner.sum() < 20 or rir.sum() < 20:
        return None
    yy_grid = np.mgrid[0:ch, 0:cw][0]
    top_m = ner & (yy_grid < ch * 0.45)
    bot_m = ner & (yy_grid >= ch * 0.45)
    prev = None
    net_sig, ring_sig, dys = [], [], []
    ref = None
    for f in range(f0, f1 + 1):
        cap.set(cv2.CAP_PROP_POS_FRAMES, f)
        ok, fr = cap.read()
        if not ok:
            continue
        crop = fr[y0:y1, x0:x1]
        g = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY)
        if SCALE != 1.0:
            g = cv2.resize(g, None, fx=SCALE, fy=SCALE,
                           interpolation=cv2.INTER_AREA)
        # 相位相关估计相对参考帧的平移并补偿（平移镜头的稳像）
        if ref is None:
            ref = g
            prev = g
            net_sig.append(0.0); ring_sig.append(0.0); dys.append(0.0)
            continue
        try:
            (dx, dy), _resp = cv2.phaseCorrelate(np.float32(ref), np.float32(g))
        except Exception:
            dx = dy = 0.0
        M = np.float32([[1, 0, -dx], [0, 1, -dy]])
        gs = cv2.warpAffine(g, M, (g.shape[1], g.shape[0]),
                            flags=cv2.INTER_LINEAR,
                            borderMode=cv2.BORDER_REPLICATE)
        d = cv2.absdiff(gs, prev)
        d = cv2.GaussianBlur(d, (3, 3), 0)
        net_sig.append(float(d[ner].mean()))
        ring_sig.append(float(d[rir].mean()))
        # 竖向运动占比：网区上/下两半的能量差（球穿网是"往下带"）
        if top_m.sum() > 8 and bot_m.sum() > 8:
            dys.append(float(d[bot_m].mean() - d[top_m].mean()))
        else:
            dys.append(0.0)
        prev = gs
    if len(net_sig) < 8:
        return None
    n = np.array(net_sig); r = np.array(ring_sig); dyv = np.array(dys)
    peak = float(n.max()); pk = int(n.argmax())
    med = float(np.median(n)) + 1e-6
    # 上升时间：从 20% 峰值到峰值用了几帧
    thr20 = 0.2 * peak
    rise = pk - int(np.argmax(n[:pk + 1] < thr20)) if pk > 0 else 0
    # 衰减：峰值后回落到 20% 峰值用了几帧
    after = n[pk:]
    below = np.where(after < thr20)[0]
    decay = int(below[0]) if len(below) else len(after)
    # 振荡：峰值后的局部极大个数
    osc = 0
    for i in range(1, len(after) - 1):
        if after[i] > after[i - 1] and after[i] >= after[i + 1] and \
                after[i] > 0.25 * peak:
            osc += 1
    return {
        "peak": round(peak, 2),
        "ratio": round(peak / (float(r.max()) + 1e-6), 2),
        "sharp": round(peak / med, 2),
        "rise": rise, "decay": decay, "osc": osc,
        "dy": round(float(dyv.max()), 2),
        "n": len(n),
    }


FEATS = ["peak", "ratio", "sharp", "rise", "decay", "osc", "dy"]


def logistic_loo(X, y, l2=1.0, iters=3000):
    """手写逻辑回归 + 留一法，返回每个样本的样本外分数。"""
    mu, sd = X.mean(0), X.std(0) + 1e-6
    Z = np.hstack([(X - mu) / sd, np.ones((len(X), 1))])
    scores = np.zeros(len(y))
    for i in range(len(y)):
        tr = np.ones(len(y), bool); tr[i] = False
        if y[tr].sum() == 0 or y[tr].sum() == tr.sum():
            scores[i] = 0.5
            continue
        w = np.zeros(Z.shape[1])
        pos = max(1.0, y[tr].sum()); neg = max(1.0, (1 - y[tr]).sum())
        cw = np.where(y[tr] > 0.5, neg / pos, 1.0)
        for _ in range(iters):
            z = Z[tr] @ w
            p = 1 / (1 + np.exp(-np.clip(z, -30, 30)))
            g = Z[tr].T @ (cw * (p - y[tr])) / tr.sum() + \
                l2 * np.r_[w[:-1], 0.0] / tr.sum()
            w -= 0.5 * g
        z = Z[i] @ w
        scores[i] = 1 / (1 + np.exp(-np.clip(z, -30, 30)))
    return scores


def main() -> int:
    d = json.loads(CANDS.read_text(encoding="utf-8"))
    cands = d["candidates"]
    fb = [json.loads(x) for x in
          FEEDBACK.read_text(encoding="utf-8").splitlines() if x.strip()]
    fb = [r for r in fb if "bili" in str(r.get("video", ""))]
    rows = []
    cap = cv2.VideoCapture(str(VID))
    fps = cap.get(cv2.CAP_PROP_FPS)
    seen = set()
    for c in cands:
        lab = None
        for r in fb:
            if abs(float(r["t"]) - float(c["t0"])) < 1.0:
                lab = r["label"]
                break
        if not lab or c["t0"] in seen:
            continue
        seen.add(c["t0"])
        hoop = (c["hoop"][0], c["hoop"][1], c["hoop"][2], c["hoop"][3])
        f = window_features(cap, fps, float(c["t0"]), hoop)
        if f is None:
            continue
        rows.append({"t": float(c["t0"]), "label": lab, **f})
    cap.release()
    print(f"样本 {len(rows)} 个（正 {sum(1 for r in rows if r['label']=='made')}）")
    print(f"{'t':>9} {'标注':>5} " + " ".join(f"{k:>7}" for k in FEATS))
    for r in rows:
        print(f"{r['t']:9.2f} {r['label']:>5} "
              + " ".join(f"{r[k]:>7}" for k in FEATS))

    X = np.array([[r[k] for k in FEATS] for r in rows], dtype=float)
    y = np.array([1.0 if r["label"] == "made" else 0.0 for r in rows])
    sc = logistic_loo(X, y)
    pos, neg = sc[y > 0.5], sc[y < 0.5]
    auc = float(((pos[:, None] > neg[None, :]).sum() +
                 0.5 * (pos[:, None] == neg[None, :]).sum())
                / (len(pos) * len(neg))) if len(pos) and len(neg) else float("nan")
    print(f"\n留一法 AUC = {auc:.3f}")
    best = (0.0, 0.5, 0.0, 0.0)
    for th in np.unique(np.round(sc, 3)):
        tp = int((pos >= th).sum()); fp = int((neg >= th).sum())
        fn = len(pos) - tp; tn = len(neg) - fp
        p = tp / (tp + fp) if tp + fp else 0.0
        rc = tp / (tp + fn) if tp + fn else 0.0
        f1 = 2 * p * rc / (p + rc) if p + rc else 0.0
        if f1 > best[0]:
            best = (f1, float(th), p, rc)
    print(f"最优 F1 {best[0]:.2f}（阈值 {best[1]:.2f}，精确率 {best[2]:.0%}，"
          f"召回 {best[3]:.0%}）")
    print("对比：只看网动能峰值 → F1 0.65/精确率48%；几何判据 → F1 0.29")
    (ROOT / "out" / "net_signature.json").write_text(
        json.dumps({"rows": rows, "auc": auc, "f1": best[0],
                    "threshold": best[1], "precision": best[2],
                    "recall": best[3], "scores": [round(float(v), 3) for v in sc]},
                   ensure_ascii=False, indent=2), encoding="utf-8")
    print("明细已存 out/net_signature.json")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
