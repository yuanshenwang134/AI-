"""用人工标注反推判据阈值 —— 让「进球判定」有真值、可量化，而不是拍脑袋。

流程（配合 scripts/label_baskets.py）：
  1. `label_baskets.py` 扫出所有候选并出证据图，你逐张判「进球 / 没进 / 看不清」，
     结果落在 `labels/basket_labels.jsonl`；
  2. 本脚本拿这些标注当**真值**，在候选的球块轨迹上重放 `_judge_span`，
     网格搜索几个关键阈值，输出 precision / recall / F1；
  3. 选 F1 最高的一组，打印出来（并可写入 --save 供管线使用）。

为什么要在「候选项」上算指标：
  阈值只影响「候选中哪些被认成进球」。整片里还有大量不含球块的帧，
  它们不会被判成进球，所以 precision/recall 在候选集上算既公平又省时间。

用法：
    python scripts\\tune_basket_rules.py --candidates out\\label_nybo\\candidates.json `
        --labels out\\label_nybo\\labels\\basket_labels.jsonl
    # 只看某一组阈值的效果：
    python scripts\\tune_basket_rules.py ... --cup-inner 0.62 --min-cup-frames 2
"""
from __future__ import annotations

import argparse
import itertools
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))


def load_labels(path: str) -> dict[int, str]:
    """读标注：{候选序号: made/miss/unclear}。"""
    out = {}
    p = Path(path)
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        d = json.loads(line)
        if d.get("label"):
            out[int(d["idx"])] = str(d["label"])
    return out


def judge_with(chain, hoop_px, cfg):
    """用给定阈值对一个候选判「是不是进球」，返回 (bool, 说明)。"""
    from aihoop.hoop import Hoop
    from aihoop.hoopsight import _judge_span

    h = Hoop(cx=hoop_px[0], cy=hoop_px[1], rx=hoop_px[2], ry=hoop_px[3],
             votes=1, confidence=1.0, method="manual")
    shot = _judge_span(chain, h, cfg)
    if shot is None:
        return False, ""
    return True, shot.note


def evaluate(cands, labels, cfg) -> dict:
    tp = fp = fn = tn = 0
    detail = []
    for i, c in enumerate(cands):
        lab = labels.get(i)
        if lab not in ("made", "miss"):
            continue
        got, _note = judge_with(c["chain"], tuple(c["hoop"]), cfg)
        truth = (lab == "made")
        if got and truth:
            tp += 1
        elif got and not truth:
            fp += 1
        elif (not got) and truth:
            fn += 1
        else:
            tn += 1
        detail.append((i, c["t0"], lab, got))
    prec = tp / (tp + fp) if (tp + fp) else 0.0
    rec = tp / (tp + fn) if (tp + fn) else 0.0
    f1 = 2 * prec * rec / (prec + rec) if (prec + rec) else 0.0
    return {"tp": tp, "fp": fp, "fn": fn, "tn": tn,
            "precision": round(prec, 3), "recall": round(rec, 3),
            "f1": round(f1, 3), "detail": detail}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="用人工标注调进球判据阈值")
    ap.add_argument("--candidates", required=True)
    ap.add_argument("--labels", required=True)
    ap.add_argument("--save", default=None, help="把最优阈值写到这个 json")
    # 单组模式
    ap.add_argument("--cup-inner", type=float, default=None)
    ap.add_argument("--min-cup-frames", type=int, default=None)
    ap.add_argument("--cross-inner", type=float, default=None)
    ap.add_argument("--enter-side-frac", type=float, default=None)
    args = ap.parse_args(argv)

    from aihoop.hoopsight import SightConfig

    data = json.loads(Path(args.candidates).read_text(encoding="utf-8"))
    cands = data["candidates"]
    labels = load_labels(args.labels)
    n_made = sum(1 for v in labels.values() if v == "made")
    n_miss = sum(1 for v in labels.values() if v == "miss")
    n_unc = sum(1 for v in labels.values() if v == "unclear")
    print(f"候选 {len(cands)} 个；标注：进球 {n_made} / 没进 {n_miss} / 看不清 {n_unc}")
    if n_made + n_miss == 0:
        print("没有可用的标注（至少要有进球和没进各一条）。先跑 label_baskets.py 标注。")
        return 2

    # 单组模式
    if any(v is not None for v in (args.cup_inner, args.min_cup_frames,
                                   args.cross_inner, args.enter_side_frac)):
        cfg = SightConfig()
        if args.cup_inner is not None:
            cfg.cup_inner = args.cup_inner
        if args.min_cup_frames is not None:
            cfg.min_cup_frames = args.min_cup_frames
        if args.cross_inner is not None:
            cfg.cross_inner = args.cross_inner
        if args.enter_side_frac is not None:
            cfg.enter_side_frac = args.enter_side_frac
        res = evaluate(cands, labels, cfg)
        print(f"阈值 cup_inner={cfg.cup_inner} min_cup_frames={cfg.min_cup_frames} "
              f"cross_inner={cfg.cross_inner} enter_side_frac={cfg.enter_side_frac}")
        print(f"  精确率 {res['precision']:.0%}  召回 {res['recall']:.0%}  "
              f"F1 {res['f1']:.2f}  (TP{res['tp']} FP{res['fp']} "
              f"FN{res['fn']} TN{res['tn']})")
        for i, t0, lab, got in res["detail"]:
            mark = "✓" if (got == (lab == "made")) else "✗"
            print(f"    {mark} idx{i} t={t0:.2f} 标注={lab} 判定={'进球' if got else '没进'}")
        return 0

    # 网格搜索
    grid = {
        "cup_inner": [0.3, 0.45, 0.62, 0.75, 0.9],
        "min_cup_frames": [1, 2, 3, 4],
        "cross_inner": [0.5, 0.7, 0.9],
        "enter_side_frac": [0.8, 1.2, 1.6],
    }
    keys = list(grid)
    rows = []
    for combo in itertools.product(*(grid[k] for k in keys)):
        cfg = SightConfig()
        for k, v in zip(keys, combo):
            setattr(cfg, k, v)
        res = evaluate(cands, labels, cfg)
        rows.append((res["f1"], res["precision"], res["recall"], dict(zip(keys, combo)),
                     res))
    rows.sort(key=lambda r: (-r[0], -r[1]))
    print(f"\n共试了 {len(rows)} 组阈值，按 F1 排序（前 10）：")
    print(f"{'F1':>5} {'精确':>6} {'召回':>6}  cup_inner min_cup cross enter_side")
    for f1, prec, rec, combo, _res in rows[:10]:
        print(f"{f1:5.2f} {prec:6.0%} {rec:6.0%}  {combo['cup_inner']:>9} "
              f"{combo['min_cup_frames']:>7} {combo['cross_inner']:>5} "
              f"{combo['enter_side_frac']:>10}")
    best_f1, best_prec, best_rec, best_combo, best_res = rows[0]
    print(f"\n最优：{best_combo}")
    print(f"  精确率 {best_prec:.0%}  召回 {best_rec:.0%}  F1 {best_f1:.2f}")
    for i, t0, lab, got in best_res["detail"]:
        mark = "✓" if (got == (lab == "made")) else "✗"
        print(f"    {mark} idx{i} t={t0:.2f} 标注={lab} 判定={'进球' if got else '没进'}")

    # 现在的默认值对照
    cur = evaluate(cands, labels, SightConfig())
    print(f"\n当前默认阈值（cup_inner=0.62 min_cup_frames=2 cross_inner=0.9 "
          f"enter_side_frac=1.6）：精确率 {cur['precision']:.0%} "
          f"召回 {cur['recall']:.0%} F1 {cur['f1']:.2f}")

    if args.save:
        Path(args.save).write_text(json.dumps(
            {"best": best_combo, "f1": best_f1, "precision": best_prec,
             "recall": best_rec, "n_labeled": n_made + n_miss,
             "current_default": {"precision": cur["precision"],
                                 "recall": cur["recall"], "f1": cur["f1"]}},
            ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"已写出：{args.save}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
