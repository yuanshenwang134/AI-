"""扫 bili_nybo.mp4 的候选并落盘（供界面逐球标注）。

用人工标定的篮筐（data/marks_bili_nybo.json），扫全片，输出：
  out/label_bili/candidates.json   —— 候选列表（含球块轨迹与几何特征）
  out/label_bili/sheets/*.png      —— 每个候选的证据图（后续也可按需生成）
"""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "src"))

from basket_label_lib import load_marks, make_sheet, scan_candidates  # noqa: E402
from aihoop.hoop import Hoop                                          # noqa: E402
from aihoop.hoopsight import SightConfig                              # noqa: E402

VID = ROOT / "data" / "bili_nybo.mp4"
MARKS = ROOT / "data" / "marks_bili_nybo.json"
OUT = ROOT / "out" / "label_bili"


def main() -> int:
    mk = load_marks(str(MARKS))
    cx, cy, rx, ry = [float(v) for v in mk["hoop"]]
    hoop = Hoop(cx=cx, cy=cy, rx=rx, ry=ry, votes=1, confidence=1.0,
                method="manual")
    cfg = SightConfig()
    cfg.stride = 1
    OUT.mkdir(parents=True, exist_ok=True)

    print(f"扫 {VID.name}（篮筐 {cx:.0f},{cy:.0f} r={rx:.0f}x{ry:.0f}）…")
    cands, meta = scan_candidates(str(VID), cfg, hoop=hoop, min_chain=3)
    print(f"候选 {len(cands)} 个")

    # 按「像进球」的程度排序：下落越大、越靠圈心 → 越可能
    def score(c):
        d = c["drop_px"] / max(1.0, ry)
        x = abs(c["rel_x_at_rim"])
        return d - 2.0 * x
    cands.sort(key=score, reverse=True)
    for i, c in enumerate(cands):
        c["rank"] = i
        c["likely"] = bool(c["drop_px"] > ry * 1.5 and abs(c["rel_x_at_rim"]) < 0.5)

    (OUT / "candidates.json").write_text(
        json.dumps({"meta": meta, "candidates": cands}, ensure_ascii=False,
                   indent=1), encoding="utf-8")
    n_likely = sum(1 for c in cands if c["likely"])
    print(f"落盘 {OUT / 'candidates.json'}；其中「像进球」的 {n_likely} 个")
    for c in cands[:20]:
        flag = "★" if c["likely"] else " "
        print(f" {flag} t={c['t0']:8.2f}  下落 {c['drop_px']:7.1f}px  "
              f"居圈心 {c['rel_x_at_rim']:+.2f}rx  n={c['n']:2d}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
