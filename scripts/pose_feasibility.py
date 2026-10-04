"""③ 可行性验证：只看**球员动作**（不看球）能不能判出进球。

背景：
  无记分牌的素材上，十条"追球"的路全部失败（球只有 7~15px，实测）。
  唯一没试过的是**换信号** —— 不看球，看**人的动作**：
  投篮的人起跳、筐下的人抬头/举手、之后的攻防转换……
  这条路的吸引力在于：球员在画面里**足够大**（几十到几百像素），
  和"球只有 7px"完全是两个量级。

验证方法（有真值可用）：
  用 `bili_nybo.mp4` 里用户标注的 32 个候选（14 进球 / 18 没进）：
  1. 在每个候选时刻 ±1.5s 的窗口里跑 **YOLO 姿态**（yolo11n-pose）；
  2. 提取与"筐下动作"相关的特征：人数、起跳幅度（踝/髋的垂直速度）、
     关键点的竖直摆动、球员到篮筐的距离分布、动作能量等；
  3. 逻辑回归 + 留一法 → AUC / 精确率 / 召回。
  如果 AUC 明显高于 0.5，说明"看人"这条路有信号，值得继续。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import numpy as np                                          # noqa: E402
from ultralytics import YOLO                                # noqa: E402
import cv2                                                  # noqa: E402

VID = ROOT / "data" / "bili_nybo.mp4"
CANDS = ROOT / "out" / "label_bili" / "candidates.json"
FEEDBACK = ROOT / "data" / "basket_feedback.jsonl"
POSE_W = ROOT / "yolo11n-pose.pt"
WIN_S = 1.5          # 前后各 1.5s
STRIDE = 3           # 每 3 帧取一次（省时间，动作是慢变量）


def window_features(model, cap, fps, t_center, hoop):
    """在候选窗口里跑姿态，提取"筐下动作"特征。"""
    cx, cy = hoop[0], hoop[1]
    f0 = int(max(0.0, t_center - WIN_S) * fps)
    f1 = int((t_center + WIN_S) * fps)
    people, jumps, energies, near_ratio = [], [], [], []
    prev = {}      # 轨迹 id -> 上一帧脚点
    for f in range(f0, f1 + 1, STRIDE):
        cap.set(cv2.CAP_PROP_POS_FRAMES, f)
        ok, fr = cap.read()
        if not ok:
            continue
        r = model.predict(fr, conf=0.3, verbose=False, device="cpu")[0]
        boxes = r.boxes
        n = 0 if boxes is None else len(boxes)
        people.append(n)
        kp = None
        if getattr(r, "keypoints", None) is not None and r.keypoints is not None:
            xy = r.keypoints.xy.cpu().numpy()
            if len(xy):
                kp = xy
        # 起跳幅度：所有人体框底边（脚）的竖直速度的max（起跳=脚离地/上移）
        if boxes is not None and len(boxes):
            feet = []
            for b in boxes.xyxy.cpu().numpy():
                feet.append(((b[0] + b[2]) / 2.0, float(b[3])))
            for (fx, fy) in feet:
                key = int(fx // 40)          # 粗粒度"位置哈希"当临时 id
                if key in prev:
                    jumps.append(prev[key] - fy)   # 脚上移为正
                prev[key] = fy
            # 筐下活动比例
            near = sum(1 for (fx, fy) in feet
                       if abs(fx - cx) < 150 and abs(fy - cy) < 220)
            near_ratio.append(near / max(1, len(feet)))
        # 关键点竖直摆动（手腕 y 的极差）—— 举手/投篮
        if kp is not None and len(kp):
            wy = kp[:, :, 1]
            energies.append(float(np.nanmax(wy) - np.nanmin(wy)))
    if len(people) < 5:
        return None
    j = np.array(jumps) if jumps else np.array([0.0])
    return {
        "n_mean": round(float(np.mean(people)), 2),
        "n_max": int(np.max(people)),
        "jump_max": round(float(np.max(j)), 2),
        "jump_p90": round(float(np.percentile(j, 90)), 2),
        "near_mean": round(float(np.mean(near_ratio)) if near_ratio else 0.0, 3),
        "swing": round(float(np.mean(energies)) if energies else 0.0, 1),
    }


FEATS = ["n_mean", "n_max", "jump_max", "jump_p90", "near_mean", "swing"]


def logistic_loo(X, y, l2=1.0, iters=3000):
    mu, sd = X.mean(0), X.std(0) + 1e-6
    Z = np.hstack([(X - mu) / sd, np.ones((len(X), 1))])
    sc = np.zeros(len(y))
    for i in range(len(y)):
        tr = np.ones(len(y), bool); tr[i] = False
        if y[tr].sum() == 0 or y[tr].sum() == tr.sum():
            sc[i] = 0.5
            continue
        w = np.zeros(Z.shape[1])
        pos = max(1.0, y[tr].sum()); neg = max(1.0, (1 - y[tr]).sum())
        cw = np.where(y[tr] > 0.5, neg / pos, 1.0)
        for _ in range(iters):
            p = 1 / (1 + np.exp(-np.clip(Z[tr] @ w, -30, 30)))
            w -= 0.5 * (Z[tr].T @ (cw * (p - y[tr])) / tr.sum()
                        + l2 * np.r_[w[:-1], 0.0] / tr.sum())
        sc[i] = 1 / (1 + np.exp(-np.clip(Z[i] @ w, -30, 30)))
    return sc


def main() -> int:
    if not POSE_W.exists():
        print(f"[err] 缺姿态模型 {POSE_W}（先跑 下载素材与依赖.ps1）")
        return 2
    d = json.loads(CANDS.read_text(encoding="utf-8"))
    cands = d["candidates"]
    fb = [json.loads(x) for x in
          FEEDBACK.read_text(encoding="utf-8").splitlines() if x.strip()]
    fb = [r for r in fb if "bili" in str(r.get("video", ""))]
    model = YOLO(str(POSE_W))
    cap = cv2.VideoCapture(str(VID))
    fps = cap.get(cv2.CAP_PROP_FPS)

    rows, seen = [], set()
    for c in cands:
        lab = None
        for r in fb:
            if abs(float(r["t"]) - float(c["t0"])) < 1.0:
                lab = r["label"]; break
        if not lab or c["t0"] in seen:
            continue
        seen.add(c["t0"])
        feat = window_features(model, cap, fps, float(c["t0"]), c["hoop"])
        if feat is None:
            continue
        rows.append({"t": float(c["t0"]), "label": lab, **feat})
    cap.release()

    print(f"样本 {len(rows)}（进 {sum(1 for r in rows if r['label']=='made')}）")
    print(f"{'t':>9} {'标注':>5} " + " ".join(f"{k:>9}" for k in FEATS))
    for r in rows:
        print(f"{r['t']:9.2f} {r['label']:>5} "
              + " ".join(f"{r[k]:>9}" for k in FEATS))
    X = np.array([[r[k] for k in FEATS] for r in rows], float)
    y = np.array([1.0 if r["label"] == "made" else 0.0 for r in rows])
    sc = logistic_loo(X, y)
    pos, neg = sc[y > 0.5], sc[y < 0.5]
    auc = float(((pos[:, None] > neg[None, :]).sum()
                 + 0.5 * (pos[:, None] == neg[None, :]).sum())
                / (len(pos) * len(neg)))
    best = (0.0, 0.5, 0.0, 0.0)
    for th in np.unique(np.round(sc, 3)):
        tp = int((pos >= th).sum()); fp = int((neg >= th).sum())
        fn = len(pos) - tp
        p = tp / (tp + fp) if tp + fp else 0.0
        rc = tp / (tp + fn) if tp + fn else 0.0
        f1 = 2 * p * rc / (p + rc) if p + rc else 0.0
        if f1 > best[0]:
            best = (f1, float(th), p, rc)
    print(f"\n=== 只看球员动作：留一法 AUC {auc:.3f} ===")
    print(f"最优 F1 {best[0]:.2f}（阈值 {best[1]:.2f}，精确率 {best[2]:.0%}，"
          f"召回 {best[3]:.0%}）")
    print("对比：追球类方法在这段素材上 AUC 0.60、F1 0.29~0.68")
    (ROOT / "out" / "pose_feasibility.json").write_text(
        json.dumps({"rows": rows, "auc": auc, "f1": best[0],
                    "precision": best[2], "recall": best[3],
                    "scores": [round(float(v), 3) for v in sc]},
                   ensure_ascii=False, indent=2), encoding="utf-8")
    print("明细已存 out/pose_feasibility.json")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
