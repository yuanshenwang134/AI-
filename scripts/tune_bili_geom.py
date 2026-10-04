"""用用户标注的 32 个候选，量化并调优**几何判据**（不用 CNN）。

为什么换回几何判据：
  CNN 在样本外只有 25/48（接近瞎猜）—— 两种情况的切片长得太像，小 CNN 学不出来。
  但扫描器本来就采集了轨迹信息（球块的时序位置），而"进没进"在几何上是明确的：
  球心是否**真的进到圈内**、下落了多少、在圈内停留几帧。
  这些量可以量化、可以调、也可以解释 —— 比一个学不动的黑箱诚实。

做法：
  1. 读 out/label_bili/candidates.json（含每个候选的球块链 chain）+ 用户标注；
  2. 在候选的链上重放 aihoop.hoopsight._judge_span，网格搜索阈值；
  3. 报告在用户标注上的精确率/召回/F1，并列出判错的条目（可逐条人工核对）。
注意：这是**在同一段素材上调参**，所以是"拟合"而非"泛化"；
      但至少阈值来自人工真值，不是拍脑袋，且错在哪条一目了然。
"""
from __future__ import annotations

import itertools
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from aihoop.hoop import Hoop                        # noqa: E402
from aihoop.hoopsight import SightConfig, _judge_span  # noqa: E402

CANDS = ROOT / "out" / "label_bili" / "candidates.json"
FEEDBACK = ROOT / "data" / "basket_feedback.jsonl"


def load_pairs():
    """把候选和它的标注配起来（按时刻就近匹配，1 个标签可能对应多个窗口）。"""
    d = json.loads(CANDS.read_text(encoding="utf-8"))
    rows = [json.loads(x) for x in
            FEEDBACK.read_text(encoding="utf-8").splitlines() if x.strip()]
    rows = [r for r in rows if "bili" in str(r.get("video", ""))]
    pairs = []
    for c in d["candidates"]:
        t = float(c["t0"])
        lab = None
        for r in rows:
            if abs(float(r["t"]) - t) < 1.0:
                lab = r["label"]
                break
        if lab:
            pairs.append((c, lab))
    return pairs


def judge(c, cfg):
    h = Hoop(cx=c["hoop"][0], cy=c["hoop"][1], rx=c["hoop"][2],
             ry=c["hoop"][3], votes=1, confidence=1.0, method="manual")
    return _judge_span(c["chain"], h, cfg)


def score(pairs, cfg):
    tp = fp = fn = tn = 0
    detail = []
    for c, lab in pairs:
        got = judge(c, cfg) is not None
        truth = (lab == "made")
        if got and truth:
            tp += 1
        elif got and not truth:
            fp += 1
        elif truth and not got:
            fn += 1
        else:
            tn += 1
        detail.append((float(c["t0"]), lab, got))
    prec = tp / (tp + fp) if (tp + fp) else 0.0
    rec = tp / (tp + fn) if (tp + fn) else 0.0
    f1 = 2 * prec * rec / (prec + rec) if (prec + rec) else 0.0
    return {"tp": tp, "fp": fp, "fn": fn, "tn": tn, "prec": prec, "rec": rec,
            "f1": f1, "detail": detail}


def main() -> int:
    pairs = load_pairs()
    n_made = sum(1 for _c, l in pairs if l == "made")
    print(f"配对 {len(pairs)} 条（进球 {n_made} / 没进 {len(pairs) - n_made}）")

    base = score(pairs, SightConfig())
    print(f"\n当前默认阈值：精确率 {base['prec']:.0%} 召回 {base['rec']:.0%} "
          f"F1 {base['f1']:.2f} (TP{base['tp']} FP{base['fp']} "
          f"FN{base['fn']} TN{base['tn']})")

    grid = {
        "cup_inner": [0.2, 0.3, 0.4, 0.5, 0.62, 0.8],
        "min_cup_frames": [1, 2, 3, 4],
        "cross_inner": [0.3, 0.5, 0.7, 0.9],
        "min_drop_ry": [1.0, 1.5, 2.0, 3.0],
        "enter_side_frac": [0.5, 0.8, 1.2, 1.6],
    }
    keys = list(grid)
    rows = []
    for combo in itertools.product(*(grid[k] for k in keys)):
        cfg = SightConfig()
        for k, v in zip(keys, combo):
            setattr(cfg, k, v)
        r = score(pairs, cfg)
        rows.append((r["f1"], r["prec"], r["rec"], dict(zip(keys, combo)), r))
    rows.sort(key=lambda x: (-x[0], -x[1]))
    print(f"\n网格 {len(rows)} 组，按 F1 排序前 8：")
    print(f"{'F1':>5} {'精确':>6} {'召回':>6}  参数")
    for f1, p, rc, combo, _r in rows[:8]:
        print(f"{f1:5.2f} {p:6.0%} {rc:6.0%}  {combo}")

    best = rows[0]
    print(f"\n最优：F1 {best[0]:.2f}  精确率 {best[1]:.0%}  召回 {best[2]:.0%}")
    print(f"  参数 {best[3]}")
    print("\n判错的条目（便于人工核对）：")
    n_bad = 0
    for t, lab, got in best[4]["detail"]:
        ok = (got == (lab == "made"))
        if not ok:
            n_bad += 1
            print(f"  ✗ t={t:8.2f}s  标注={lab:4s}  判据={'进球' if got else '没进'}")
    print(f"  共错 {n_bad} 条 / {len(pairs)}")

    (ROOT / "out" / "tune_bili_geom.json").write_text(json.dumps(
        {"best": best[3], "f1": best[0], "precision": best[1], "recall": best[2],
         "n": len(pairs), "n_made": n_made,
         "baseline": {"prec": base["prec"], "rec": base["rec"], "f1": base["f1"]}},
        ensure_ascii=False, indent=2), encoding="utf-8")
    print("\n已存 out/tune_bili_geom.json")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
