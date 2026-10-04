"""从视频里自举比分牌图案模板 —— 只跑一次，产物是 data/scoreboard_templates.npz。

**做法：不识别数字，直接对「字段窗口」聚类。**

  固定机位 + 固定台标下，**同一个比分值在每一帧的像素几乎完全相同**。
  所以把主队字段那块小窗口整块抠出来聚类，一类就是一个比分值
  （"2"、"4"、"7"、"10"、"13"...），匹配距离接近 0。

  对比一下逐字识别：要先切字符（两位数还会粘在一起）、再归一化
  （缩放到统一尺寸会把宽度信息抹掉，「1」和「7」就混了）、再比数字模板。
  三件事任何一件出错整条链就废。窗口图案法把这三件事一次绕开。

  唯一代价是「每个频道要人工标注一次图案」。脚本会把每类的样本拼成
  contact sheet（`--sheet`），你照着图把数值填进 `--labels-home/away/period`
  即可，几十秒的事。同一个频道的下一场视频可以直接 `--reference` 继承标签。

用法：
    # 1) 先出一张对照图看看有哪些图案
    python scripts/build_scoreboard_templates.py --video data/basketball_match.mp4

    # 2) 按图上的顺序填数值，正式生成模板
    python scripts/build_scoreboard_templates.py --video data/basketball_match.mp4 \
        --labels-home 2,4,7,9,10,13 --labels-away 5,6 --labels-period 1
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from aihoop.scoreboard import (  # noqa: E402
    FieldPatterns, ScoreBug, ScoreBugConfig, calibrate_fields, field_window,
    locate_score_bug, _require_cv,
)


# --------------------------------------------------------------------------
# 聚类
# --------------------------------------------------------------------------
def cluster(vecs, thr: float, passes: int = 8):
    """贪心凝聚聚类 + 若干轮重分配。返回 [(簇心, 成员下标)]。

    同一个比分值的窗口在不同帧几乎逐像素相同（距离 <0.02），
    不同比分值之间距离 >0.15，所以阈值放在 0.05~0.09 都很稳。
    """
    _cv2, np = _require_cv()
    if not vecs:
        return []
    X = np.stack(vecs)
    cents = [X[0].copy()]
    counts = [1]
    for v in X[1:]:
        d = [float(np.sqrt(((c - v) ** 2).mean())) for c in cents]
        j = int(np.argmin(d))
        if d[j] < thr:
            counts[j] += 1
            cents[j] = cents[j] + (v - cents[j]) / counts[j]
        else:
            cents.append(v.copy())
            counts.append(1)
    cents = np.stack(cents)

    assign = np.full(len(X), -1, dtype=int)
    for _ in range(passes):
        D = np.sqrt(((X[:, None, :, :] - cents[None, :, :, :]) ** 2)
                    .reshape(len(X), len(cents), -1).mean(axis=2))
        new_assign = D.argmin(axis=1)
        new_cents = []
        for k in range(len(cents)):
            sel = X[new_assign == k]
            new_cents.append(sel.mean(axis=0) if len(sel) else cents[k])
        cents = np.stack(new_cents)
        if np.array_equal(new_assign, assign):
            assign = new_assign
            break
        assign = new_assign

    out = []
    for k in range(len(cents)):
        members = np.where(assign == k)[0].tolist()
        if members:
            out.append((cents[k], members))
    return out


def order_clusters(clusters, times):
    """按「首次出现时间」排序 —— 和比分推进的顺序一致，人工核对最直观。"""
    def first_seen(c):
        return min(times[i] for i in c[1])
    return sorted(clusters, key=first_seen)


# --------------------------------------------------------------------------
# 主流程
# --------------------------------------------------------------------------
def _parse_vals(spec: str) -> list[int]:
    """解析 --labels-*：逗号分隔的比分值；`x` 表示「这一帧画面上没有比分牌」。

    `x` 会以 -1 存进模板库，读取时被判为「读不到」而不是「认成别的数字」。
    这很重要：比分牌在回放/切换镜头时会整块消失，那些空白窗口如果从模板库里
    删掉，它会去和某个真实数字比距离，然后给出一个错误的比分。
    """
    out: list[int] = []
    for tok in str(spec).split(","):
        tok = tok.strip().lower()
        if tok in ("x", "-", "?", ""):
            out.append(-1)
        else:
            try:
                out.append(int(tok))
            except ValueError:
                out.append(-1)
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="从视频自举比分牌字段图案模板")
    ap.add_argument("--video", required=True)
    ap.add_argument("--out", default="data/scoreboard_templates.npz")
    ap.add_argument("--bug-out", default="data/scoreboard_bug.json")
    ap.add_argument("--sheet", default="out/scoreboard_templates.png")
    ap.add_argument("--frames", type=int, default=300, help="抽多少帧取图案")
    ap.add_argument("--stride", type=int, default=3)
    ap.add_argument("--cluster-thr", type=float, default=0.07)
    ap.add_argument("--min-count", type=int, default=2)
    ap.add_argument("--labels-home", default=None, help="主队字段：按对照图顺序填，如 2,4,7,10,13")
    ap.add_argument("--labels-away", default=None, help="客队字段")
    ap.add_argument("--labels-period", default=None, help="节次字段，如 1")
    ap.add_argument("--reference", default=None, help="已有模板 npz：同频道可直接继承标签")
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args(argv)

    cv2, np = _require_cv()
    cfg = ScoreBugConfig(stride=args.stride)

    print(f"[1/4] 定位比分牌 + 标定字段：{args.video}")
    bug = locate_score_bug(args.video, cfg)
    print("      " + json.dumps(bug.to_dict(), ensure_ascii=False))

    print(f"[2/4] 抽帧取三个字段的窗口（{args.frames} 帧）…")
    cap = cv2.VideoCapture(args.video)
    total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
    idxs = np.linspace(0, max(0, total - 2), args.frames).astype(int)

    raw: dict[str, list] = {"home": [], "away": [], "period": []}
    times: dict[str, list] = {"home": [], "away": [], "period": []}
    for i in idxs:
        cap.set(cv2.CAP_PROP_POS_FRAMES, int(i))
        ok, frame = cap.read()
        if not ok:
            continue
        t = float(i) / (cap.get(cv2.CAP_PROP_FPS) or 30.0)
        for name, cx in (("home", bug.home_cx), ("away", bug.away_cx),
                         ("period", bug.period_cx)):
            w = field_window(frame, bug, cx, cfg)
            if w is not None and w.shape[0] >= 6 and w.shape[1] >= 8:
                raw[name].append(w)
                times[name].append(t)
    cap.release()

    print("[3/4] 聚类…")
    ref = None
    ref_path = args.reference or args.out
    if Path(ref_path).exists():
        try:
            ref = FieldPatterns.load(ref_path)
        except Exception:
            ref = None

    parsed: dict[str, list[tuple[object, list[int]]]] = {}
    labels: dict[str, list[str]] = {}
    manual = {"home": args.labels_home, "away": args.labels_away,
              "period": args.labels_period}
    for name in ("home", "away", "period"):
        vecs = raw[name]
        if len(vecs) < 4:
            parsed[name] = []
            labels[name] = []
            print(f"      {name}: 窗口太少（{len(vecs)}），跳过")
            continue
        cl = [c for c in cluster(vecs, args.cluster_thr)
              if len(c[1]) >= args.min_count]
        cl = order_clusters(cl, times[name])
        parsed[name] = cl
        spec = manual[name]
        if spec:
            vals = _parse_vals(spec)
            lab = [str(vals[k]) if k < len(vals) else "" for k in range(len(cl))]
            src = "人工"
        elif ref is not None and ref.has(name):
            lab = _inherit(ref, name, cl, np)
            src = f"继承({ref_path})"
        else:
            lab = ["" for _ in cl]
            src = "未标注"
        labels[name] = lab
        sizes = ", ".join(
            f"#{k}x{len(c[1])}->{('无' if lab[k] == '-1' else lab[k]) or '?'}"
            for k, c in enumerate(cl))
        print(f"      {name}: {len(cl)} 类 [{src}]  {sizes}")

    def reps_of(name):
        items = [(c[0], l) for c, l in zip(parsed[name], labels[name]) if l != ""]
        if not items:
            return None, []
        return (np.stack([r for r, _l in items]),
                [int(l) for _r, l in items])

    hr, hv = reps_of("home")
    ar, av = reps_of("away")
    pr, pv = reps_of("period")
    fp = FieldPatterns(home_reps=hr, home_vals=hv, away_reps=ar,
                       away_vals=av, period_reps=pr, period_vals=pv)

    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    fp.to_npz(args.out)
    print(f"      [ok] {args.out}")
    Path(args.bug_out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.bug_out).write_text(
        json.dumps(bug.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"      [ok] {args.bug_out}")

    print("[4/4] 出对照图（照着它填 --labels-*）…")
    try:
        _sheet(parsed, labels, raw, args.sheet)
        print(f"      [ok] {args.sheet}")
    except Exception as e:  # noqa: BLE001
        print(f"      [warn] 出图失败：{e}")

    if not any(fp.home_vals) or not fp.away_vals:
        print("\n[!] 还没有标注。打开对照图，按每一类从上到下的顺序把比分值填进来：")
        print("    python scripts/build_scoreboard_templates.py "
              f"--video {args.video} --labels-home 2,4,7,10,13 "
              "--labels-away 5,6 --labels-period 1")
        return 1
    print("\n[ok] 模板就绪，可以跑分析了。")
    return 0


def _inherit(ref: FieldPatterns, name: str, clusters, np) -> list[str]:
    """同频道复用：把已有模板的数值按最近邻搬过来。"""
    R = np.asarray(getattr(ref, f"{name}_reps"), dtype=np.float32)
    vals = getattr(ref, f"{name}_vals")
    out = []
    for rep, _members in clusters:
        if R.shape[1:] != rep.shape:
            out.append("")
            continue
        d = np.sqrt(((R - rep) ** 2).reshape(len(vals), -1).mean(axis=1))
        j = int(np.argmin(d))
        out.append(str(vals[j]) if float(d[j]) < 0.05 else "")
    return out


def _sheet(parsed, labels, raw, path: str) -> None:
    """每个字段每一类：簇心 + 若干真实样本横铺，左边标出类号和标签。"""
    cv2, np = _require_cv()
    K = 3
    blocks = []
    for name in ("home", "away", "period"):
        cl = parsed.get(name) or []
        if not cl:
            continue
        head = np.full((30, 720, 3), 30, np.uint8)
        cv2.putText(head, f"== {name} field ==", (8, 22),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 255, 255), 2)
        blocks.append(head)
        for k, (rep, members) in enumerate(cl):
            imgs = [rep] + [raw[name][i] for i in members[:9]]
            row = np.hstack([
                np.pad(np.kron((np.clip(t, 0, 1) * 255).astype(np.uint8),
                               np.ones((K, K), np.uint8)),
                       ((2, 2), (2, 2)), constant_values=70)
                for t in imgs])
            row = cv2.cvtColor(row, cv2.COLOR_GRAY2BGR)
            row = cv2.copyMakeBorder(row, 26, 4, 4, 4, cv2.BORDER_CONSTANT,
                                     value=(45, 45, 45))
            cv2.putText(row, f"#{k} -> {labels[name][k] or '?'}  n={len(members)}",
                        (8, 20), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0, 255, 255), 2)
            blocks.append(row)
            blocks.append(np.full((6, row.shape[1], 3), 90, np.uint8))
    if not blocks:
        return
    width = max(b.shape[1] for b in blocks)
    blocks = [np.pad(b, ((0, 0), (0, width - b.shape[1]), (0, 0)))
              for b in blocks]
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    cv2.imwrite(path, np.vstack(blocks))


if __name__ == "__main__":
    raise SystemExit(main())
