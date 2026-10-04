"""用「用户提供的那段视频」导训练数据 —— nybo_3min.mp4。

为什么改用它（而不是转播素材 basketball_match）：
  1. 用户明确要求"用我提供的这一段视频来训练"；
  2. 这段有**确定的真值**（用户逐帧确认：173.42s 唯一进球，其余 6 个候选没进）；
  3. 这段有**人工标定的篮筐**（比转播素材那种"我用眼睛估"的位置可靠得多）；
  4. 转播素材上篮筐位置一直没定准（实测发现用球场标定反算会算到比分牌上），
     继续用它会重演"模型学错东西"的坑。

标签构造（诚实说明）：
  * 正样本：进球时刻附近的时间抖动（±0.2s，5 个）。切片中心用人工标定的篮筐。
    时间抖动制造的是"同一次进球的不同相位"，不是不同的球 —— 所以
    **必须在样本数上标明这一点**，不能当成 5 个独立进球。
  * 负样本：其余 6 个候选各自的抖动 + 全片随机抽样（同一篮筐窗口）。
    随机抽样里可能混有真进球（这段真值只有 1 个球，但采样窗口可能擦到它），
    所以会**排除进球时刻 ±3s** 的采样。

输出：out/basket_ds_nybo/（meta.jsonl + patches/）
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "src"))

from basket_label_lib import require_cv, load_marks          # noqa: E402
from export_basket_patches import export_patch, net_features  # noqa: E402

VID = ROOT / "data" / "nybo_3min.mp4"
MARKS = ROOT / "data" / "marks_nybo_3min.json"
CANDS = ROOT / "out" / "label_nybo" / "candidates.json"
GOAL_T = 173.42          # 用户逐帧确认的唯一进球


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="用用户提供的素材导训练数据")
    ap.add_argument("--out", default="out/basket_ds_nybo")
    ap.add_argument("--jitter", type=float, default=0.2)
    ap.add_argument("--n-jitter", type=int, default=5)
    ap.add_argument("--random-neg", type=int, default=60)
    args = ap.parse_args(argv)

    cv2, np = require_cv()
    out = Path(args.out)
    (out / "patches").mkdir(parents=True, exist_ok=True)

    mk = load_marks(str(MARKS))
    if not mk or not mk.get("hoop"):
        print(f"[err] 缺少人工标点 {MARKS}（在界面上标一次篮筐即可）")
        return 2
    cx, cy, rx, ry = [float(v) for v in mk["hoop"]]
    print(f"篮筐（人工标定）：cx={cx:.0f} cy={cy:.0f} rx={rx:.0f} ry={ry:.0f}")

    cands = json.loads(CANDS.read_text(encoding="utf-8"))["candidates"]
    cap = cv2.VideoCapture(str(VID))
    fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
    total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
    dur = total / fps if fps else 0.0
    print(f"素材 {VID.name}：{dur:.1f}s @ {fps:.2f}fps，候选 {len(cands)} 个，"
          f"真值进球 {GOAL_T:.2f}s")

    rows = []

    def add(t: float, label: int, tag: str):
        key = f"{tag}_t{int(round(t * 1000)):07d}"
        arr, xy, fulls = export_patch(cv2, np, cap, fps, t, cx, cy,
                                      out / "patches" / f"{key}.npy")
        if arr is None:
            return False
        feats = net_features(cv2, np, fulls[0], fulls[1], cx, cy, rx, ry)
        rows.append({"split": "train", "key": key, "video": str(VID),
                     "t": round(t, 2), "hoop_px": [cx, cy], "label": label,
                     "src": tag, "patch": f"patches/{key}.npy", **feats})
        return True

    # ---- 正样本：唯一进球时刻附近的时间抖动 ----
    n_pos = 0
    for k in range(args.n_jitter):
        off = 0.0 if k == 0 else (args.jitter * (1 if k % 2 else -1)
                                  * ((k + 1) // 2) / max(1, args.n_jitter // 2))
        if add(GOAL_T + off, 1, "goal"):
            n_pos += 1
    print(f"正样本 {n_pos} 个（都是同一次进球的相位抖动 —— 不是 {n_pos} 次独立进球）")

    # ---- 负样本 A：其余 6 个候选 ----
    n_neg = 0
    for c in cands:
        if abs(c["t0"] - GOAL_T) < 1.0:          # 跳过进球本身
            continue
        for k in range(2):
            off = (k - 0.5) * args.jitter
            if add(c["t0"] + off, 0, "cand_miss"):
                n_neg += 1
    print(f"负样本 A（其余候选）{n_neg} 个")

    # ---- 负样本 B：全片随机抽样，排除进球 ±3s ----
    rng = np.random.RandomState(7)
    n_rand = 0
    tries = 0
    while n_rand < args.random_neg and tries < args.random_neg * 8:
        tries += 1
        t = float(rng.uniform(1.0, max(2.0, dur - 1.0)))
        if abs(t - GOAL_T) < 3.0:
            continue
        if add(t, 0, "random"):
            n_rand += 1
    print(f"负样本 B（随机时刻）{n_rand} 个")
    cap.release()

    (out / "meta.jsonl").write_text(
        "\n".join(json.dumps(r, ensure_ascii=False) for r in rows),
        encoding="utf-8")
    print(f"\n已导出 {out}/meta.jsonl：总 {len(rows)} 条"
          f"（正 {sum(1 for r in rows if r['label'] == 1)} / "
          f"负 {sum(1 for r in rows if r['label'] == 0)}）")
    print("注意：正样本全部来自**同一次进球**的相位抖动，所以样本数不等于进球数；"
          "评估时必须按「同一次进球不能同时出现在训练和验证集」来分组。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
