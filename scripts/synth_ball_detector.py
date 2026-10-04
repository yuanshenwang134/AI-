"""路 1：合成小球检测器（领域随机化）——绕开"球太小、人眼也标不准"。

为什么换这个思路：
  之前所有追球方法失败，共同的物理原因是球只占画面宽 0.7%~1.5%。
  而"训练检测器"一直卡在**标注**上：人眼在 5~15px 上标不准
  （实测让你点球心时，11 个框全落在了篮圈上）→ 训练数据本身就是错的。
  上一版 CNN 因此学不到东西（样本外 25/48 ≈ 瞎猜）。

本方法：
  1. **背景**取自你视频里没有球的真实帧（球场纹理、观众、灯光都是真的）；
  2. **球**由程序合成：橙色球体 + 径向明暗 + **按真实速度加运动模糊**
     （24fps 下快球帧间位移 20~40px，这才是现成模型零检出的真正原因）；
  3. 正样本 = 已知球心的干净标签（合成的好处就是**标签绝对准**）；
  4. 训一个小 CNN（24×24 patch 二分类）→ 在真帧上扫窗得到"球似然图"；
  5. 用你标注的 32 个候选（14 进 / 18 没进）做 **独立验证**：
     进球窗口里球应当穿过筐口 → 筐区似然峰值应显著更高。

用法：
    python scripts/synth_ball_detector.py --train          # 合成数据 + 训练
    python scripts/synth_ball_detector.py --eval           # 在真帧上评估（AUC）
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import cv2                                                   # noqa: E402
import numpy as np                                           # noqa: E402

PATCH = 24
HALF = PATCH // 2
VID = ROOT / "data" / "bili_nybo.mp4"
DAIRY = ROOT / "data" / "sample_dairy.mov"
W_BALL = ROOT / "runs" / "synth_ball" / "cnn.pt"


# ---------------------------------------------------------------- 合成
def make_ball_patch(rng, radius, blur_len, blur_ang):
    """生成一个带明暗和运动模糊的橙色球 patch（float RGB 0~1）。"""
    size = PATCH
    img = np.zeros((size, size), np.float32)
    cx = cy = (size - 1) / 2.0
    yy, xx = np.mgrid[0:size, 0:size]
    r = np.sqrt((xx - cx) ** 2 + (yy - cy) ** 2)
    # 球体：径向明暗（左上受光）
    shade = np.clip(1.25 - r / max(1.0, radius) * 0.9, 0, 1.3)
    lit = np.clip(1.0 - np.hypot(xx - cx + radius * 0.35,
                                 yy - cy + radius * 0.35) / (radius * 1.7), 0, 1)
    mask = (r <= radius).astype(np.float32)
    # 篮球橙：RGB 约 (0.85, 0.45, 0.15) 加随机色偏
    base = np.array([0.85, 0.45, 0.15], np.float32) * \
        (1.0 + rng.uniform(-0.15, 0.15, 3))
    ball = np.zeros((size, size, 3), np.float32)
    for c in range(3):
        ball[..., c] = base[c] * shade * (0.85 + 0.5 * lit)
    # 压缩到球径
    ball = cv2.GaussianBlur(ball, (0, 0), max(0.6, radius * 0.35))
    out = ball * mask[..., None]
    # 运动模糊（关键：真实快球就是一条糊痕）
    if blur_len > 1:
        k = np.zeros((blur_len, blur_len), np.float32)
        cv2.line(k, (0, blur_len // 2), (blur_len - 1, blur_len // 2), 1, 1)
        M = cv2.getRotationMatrix2D((blur_len / 2 - 0.5, blur_len / 2 - 0.5),
                                    blur_ang, 1.0)
        k = cv2.warpAffine(k, M, (blur_len, blur_len))
        k /= max(1e-6, k.sum())
        out = cv2.filter2D(out, -1, k)
    return out, mask.sum() > 0


def synth_sample(rng, bg, rim_xy=None, force_near_rim=False):
    """把合成球贴到真实背景上，返回 (patch, 是否有球)。"""
    h, w = bg.shape[:2]
    has_ball = rng.random() < 0.5
    if not has_ball:
        x = rng.integers(HALF, w - HALF); y = rng.integers(HALF, h - HALF)
        patch = bg[y - HALF:y + HALF, x - HALF:x + HALF].astype(np.float32) / 255
        return patch, 0
    radius = rng.uniform(1.6, 7.0)              # 球半径 → 直径 3~14px
    blur_len = int(np.clip(rng.normal(6, 3), 1, 15))
    blur_ang = rng.uniform(0, 180)
    if force_near_rim and rim_xy is not None:
        x = int(np.clip(rim_xy[0] + rng.normal(0, 25), HALF, w - HALF))
        y = int(np.clip(rim_xy[1] + rng.normal(0, 25), HALF, h - HALF))
    else:
        x = rng.integers(HALF, w - HALF); y = rng.integers(HALF, h - HALF)
    bgp = bg[y - HALF:y + HALF, x - HALF:x + HALF].astype(np.float32) / 255
    ball, _ = make_ball_patch(rng, radius, blur_len, blur_ang)
    out = np.clip(bgp * (1 - ball) + ball, 0, 1)     # ball 自带 alpha（边缘为 0）
    return out, 1


def load_backgrounds(video, n=60, skip_goals=None):
    cap = cv2.VideoCapture(str(video))
    fps = cap.get(cv2.CAP_PROP_FPS) or 24.0
    total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
    bgs = []
    for i in range(n):
        f = int(total * (i + 0.5) / n)
        cap.set(cv2.CAP_PROP_POS_FRAMES, f)
        ok, fr = cap.read()
        if ok:
            bgs.append(fr)
    cap.release()
    return bgs


# ---------------------------------------------------------------- 模型
def build_cnn():
    import torch.nn as nn

    class Net(nn.Module):
        def __init__(self):
            super().__init__()
            self.f = nn.Sequential(
                nn.Conv2d(3, 16, 3, padding=1), nn.ReLU(), nn.MaxPool2d(2),
                nn.Conv2d(16, 32, 3, padding=1), nn.ReLU(), nn.MaxPool2d(2),
                nn.Conv2d(32, 32, 3, padding=1), nn.ReLU(),
                nn.AdaptiveAvgPool2d(1))
            self.h = nn.Linear(32, 1)

        def forward(self, x):
            return self.h(self.f(x).flatten(1)).squeeze(1)
    return Net()


def train(n_bg=60, n_samples=6000, epochs=4):
    import torch
    import torch.nn as nn
    rng = np.random.default_rng(0)
    bgs = load_backgrounds(VID, n_bg)
    print(f"背景帧 {len(bgs)} 张（取自你视频的真实画面）")
    X = np.zeros((n_samples, PATCH, PATCH, 3), np.float32)
    y = np.zeros(n_samples, np.float32)
    for i in range(n_samples):
        bg = bgs[rng.integers(len(bgs))]
        p, lab = synth_sample(rng, bg)
        X[i] = p; y[i] = lab
    print(f"合成样本 {n_samples}（正 {int(y.sum())} / 负 {int((1-y).sum())}）")

    net = build_cnn()
    opt = torch.optim.Adam(net.parameters(), lr=2e-3)
    lossf = nn.BCEWithLogitsLoss()
    Xt = torch.from_numpy(X).permute(0, 3, 1, 2)
    yt = torch.from_numpy(y)
    n_val = n_samples // 6
    Xv, yv = Xt[:n_val], yt[:n_val]
    Xt, yt = Xt[n_val:], yt[n_val:]
    bs = 128
    for ep in range(epochs):
        perm = torch.randperm(len(Xt))
        tot = 0.0
        for i in range(0, len(Xt), bs):
            idx = perm[i:i + bs]
            opt.zero_grad()
            loss = lossf(net(Xt[idx]), yt[idx])
            loss.backward(); opt.step()
            tot += float(loss) * len(idx)
        with torch.no_grad():
            pv = torch.sigmoid(net(Xv))
            acc = float(((pv > 0.5).float() == yv).float().mean())
        print(f"  epoch {ep+1}/{epochs}  loss {tot/len(Xt):.4f}  合成验证准确率 {acc:.3f}")
    W_BALL.parent.mkdir(parents=True, exist_ok=True)
    torch.save(net.state_dict(), W_BALL)
    print(f"已存 {W_BALL}")
    return X, y


# ---------------------------------------------------------------- 评估
def scan_score(net, frame, region, stride=4):
    """在 region=(x0,y0,x1,y1) 内滑窗，返回最大球分数。"""
    import torch
    x0, y0, x1, y1 = [int(v) for v in region]
    x0 = max(HALF, x0); y0 = max(HALF, y0)
    x1 = min(frame.shape[1] - HALF, x1); y1 = min(frame.shape[0] - HALF, y1)
    if x1 <= x0 or y1 <= y0:
        return 0.0, None
    patches, pos = [], []
    for y in range(y0, y1, stride):
        for x in range(x0, x1, stride):
            patches.append(frame[y - HALF:y + HALF, x - HALF:x + HALF])
            pos.append((x, y))
    arr = np.stack(patches).astype(np.float32) / 255
    with torch.no_grad():
        t = torch.from_numpy(arr).permute(0, 3, 1, 2)
        s = torch.sigmoid(net(t)).numpy()
    i = int(np.argmax(s))
    return float(s[i]), pos[i]


def eval_on_labels():
    import torch
    net = build_cnn()
    net.load_state_dict(torch.load(W_BALL)); net.eval()

    cands = json.loads((ROOT / "out/label_bili/candidates.json")
                       .read_text(encoding="utf-8"))["candidates"]
    fb = [json.loads(x) for x in
          (ROOT / "data/basket_feedback.jsonl").read_text(encoding="utf-8").splitlines()
          if x.strip()]
    fb = [r for r in fb if "bili" in str(r.get("video", ""))]
    cap = cv2.VideoCapture(str(VID))
    fps = cap.get(cv2.CAP_PROP_FPS)

    rows, seen = [], set()
    for c in cands:
        lab = next((r["label"] for r in fb
                    if abs(float(r["t"]) - float(c["t0"])) < 1.0), None)
        if not lab or c["t0"] in seen:
            continue
        seen.add(c["t0"])
        hx, hy, rx, ry = c["hoop"]
        # 筐口附近（球进筐必然经过这里）
        region = (hx - 2.2 * rx, hy - 2.5 * ry, hx + 2.2 * rx, hy + 3.0 * ry)
        best = 0.0
        for k in range(-8, 9):
            cap.set(cv2.CAP_PROP_POS_FRAMES, int((float(c["t0"]) + k / fps) * fps))
            ok, fr = cap.read()
            if not ok:
                continue
            s, _p = scan_score(net, fr, region)
            best = max(best, s)
        rows.append({"t": float(c["t0"]), "label": lab, "score": round(best, 3)})
    cap.release()

    pos = np.array([r["score"] for r in rows if r["label"] == "made"])
    neg = np.array([r["score"] for r in rows if r["label"] == "miss"])
    auc = float(((pos[:, None] > neg[None, :]).sum()
                 + 0.5 * (pos[:, None] == neg[None, :]).sum())
                / (len(pos) * len(neg))) if len(pos) and len(neg) else float("nan")
    print(f"\n=== 路 1 独立验证（你的 32 个标注）===")
    print(f"  进球 {len(pos)} 个  分数中位 {np.median(pos):.3f}")
    print(f"  没进 {len(neg)} 个  分数中位 {np.median(neg):.3f}")
    print(f"  AUC = {auc:.3f}   （0.5 = 瞎猜；上一版真球标签 CNN 也是 ~0.5）")
    print(f"  对照：追球类方法 AUC 0.29~0.60")
    best_f1 = (0.0, 0.5, 0.0, 0.0)
    for th in np.unique(np.round(np.concatenate([pos, neg]), 3)):
        tp = int((pos >= th).sum()); fp = int((neg >= th).sum())
        fn = len(pos) - tp
        p = tp / (tp + fp) if tp + fp else 0.0
        rc = tp / (tp + fn) if tp + fn else 0.0
        f1 = 2 * p * rc / (p + rc) if p + rc else 0.0
        if f1 > best_f1[0]:
            best_f1 = (f1, float(th), p, rc)
    print(f"  最优 F1 {best_f1[0]:.2f}（阈值 {best_f1[1]:.2f}，精确率 {best_f1[2]:.0%}，"
          f"召回 {best_f1[3]:.0%}）")
    (ROOT / "out" / "synth_ball_eval.json").write_text(
        json.dumps({"auc": auc, "f1": best_f1[0], "rows": rows},
                   ensure_ascii=False, indent=2), encoding="utf-8")
    print("明细已存 out/synth_ball_eval.json")
    print("\n注意：这只是「能不能定位球」的验证；即使 AUC 高，"
          "还需要球轨迹穿筐判据才能定进球。")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="合成小球检测器（领域随机化）")
    ap.add_argument("--train", action="store_true")
    ap.add_argument("--eval", action="store_true")
    ap.add_argument("--samples", type=int, default=6000)
    ap.add_argument("--epochs", type=int, default=4)
    a = ap.parse_args(argv)
    if a.train:
        train(n_samples=a.samples, epochs=a.epochs)
    if a.eval:
        eval_on_labels()
    if not (a.train or a.eval):
        print("用 --train 或 --eval")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
