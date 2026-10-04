"""篮下进球标注工具（第 1 步）：扫出所有「可能在篮筐附近出现球」的时刻，并出证据图。

设计意图（这一步不训练模型，而是**把你的判断变成可量化的真值**）：
  * 我之前的判据在「篮筐贴画面顶边」的机位上误判严重（用户指出 59.94s / 127.67s
    两个假进球）。继续拍脑袋调阈值不负责任，也不可验证。
  * 所以先做**密集候选**：只要篮筐窗口里出现「球的尺寸」的运动块，就记一个候选
    （宁可多报，不要漏报），然后由你用眼睛判「进球 / 没进 / 看不清」。
  * 你的标注会写进 `labels/basket_labels.jsonl`，成为这段素材的真值；
    再用它去自标定判据（scripts/tune_basket_rules.py），并在报告里给出
    precision / recall —— 而不是我说"应该对"。

证据图（每个候选一张，横排若干帧）：
  * 只看篮筐 + 网那块（放大 2.6 倍）；
  * 画出篮圈位置；
  * 每帧标出候选球块的中心与「相对篮圈中心的横向偏移（× rx）」，
    便于判断球是「从筐里下去」还是「贴着圈外沿掠过」。

用法：
    python scripts/label_baskets.py --video data\\nybo_3min.mp4 --out out/label_nybo
    # 只出证据图不标注：
    python scripts/label_baskets.py --video data\\nybo_3min.mp4 --out out/label_nybo --no-ui
"""
from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))


def _require_cv():
    import cv2
    import numpy as np
    return cv2, np


def scan_candidates(video: str, cfg, hoop_track=None, progress=None):
    """扫全片，返回候选时刻列表（每个候选含球块轨迹 + 网动能量）。

    判定候选的门槛刻意**很松**：只要篮筐窗口里出现「球的尺寸」的块就算。
    候选多不要紧，人工判一次很快；漏了真进球才是致命的。
    """
    from aihoop.hoop import HoopConfig, detect_hoop_track
    from aihoop.hoopsight import _blobs_in_window, _pick_blob, _window

    cv2, np = _require_cv()
    if hoop_track is None:
        hoop_track = detect_hoop_track(video, HoopConfig(),
                                       weights=cfg.hoop_weights,
                                       device=cfg.device, progress=progress)
    from aihoop.hoopsight import _visible_hoops

    cap = cv2.VideoCapture(video)
    fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
    W = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    H = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    hoops = _visible_hoops(hoop_track, W, H, cfg.sight)
    if not hoops:
        cap.release()
        raise RuntimeError("这段视频里没有定位到可用的篮筐")
    hoop, samples = hoops[0]
    win = _window(cfg.sight, hoop, W, H)

    prev = None
    series = []
    idx = 0
    while True:
        ok = cap.grab()
        if not ok:
            break
        ok, frame = cap.retrieve()
        if not ok:
            break
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        if prev is not None:
            blobs = _blobs_in_window(cv2, np, prev, gray, win, hoop, cfg.sight)
            b = _pick_blob(blobs, hoop, cfg.sight)
            if b is not None:
                series.append((round(idx / fps, 3), round(b[0], 1),
                               round(b[1], 1), int(b[2])))
        prev = gray
        idx += 1
    cap.release()

    # 串成链：相邻采样间隔 <= max_gap
    gap = 3.0 / fps
    chains = []
    cur = [series[0]] if series else []
    for p in series[1:]:
        if p[0] - cur[-1][0] <= gap:
            cur.append(p)
        else:
            chains.append(cur)
            cur = [p]
    if cur:
        chains.append(cur)

    out = []
    for ch in chains:
        if len(ch) < cfg.min_chain:
            continue
        xs = [p[1] for p in ch]
        ys = [p[2] for p in ch]
        rel = [(x - hoop.cx) / max(1.0, hoop.rx) for x in xs]
        out.append({
            "t0": ch[0][0], "t1": ch[-1][0], "n": len(ch),
            "x_span": [round(min(xs), 1), round(max(xs), 1)],
            "y_span": [round(min(ys), 1), round(max(ys), 1)],
            "rel_x_min_abs": round(min(abs(r) for r in rel), 2),
            "rel_x_at_rim": round(
                min(rel, key=lambda r: abs(r)), 2),
            "drop_px": round(ys[-1] - ys[0], 1),
            "hoop": [round(hoop.cx, 1), round(hoop.cy, 1),
                     round(hoop.rx, 1), round(hoop.ry, 1)],
            "win": list(win),
            "chain": ch,
        })
    return out, {"hoop": [hoop.cx, hoop.cy, hoop.rx, hoop.ry],
                 "window": list(win), "fps": fps, "blob_samples": len(series)}


def make_sheet(video: str, cand: dict, out_png: Path, zoom: float = 2.6,
               span: float = 1.6, ncols: int = 8):
    """给一个候选出证据图：横排若干帧，标出球块与「相对篮圈的横向偏移」。"""
    cv2, np = _require_cv()
    win = cand["win"]
    cx, cy, rx, ry = cand["hoop"]
    cap = cv2.VideoCapture(video)
    fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
    t0 = max(0.0, cand["t0"] - 0.35)
    t1 = cand["t1"] + 0.55
    f0, f1 = int(t0 * fps), int(t1 * fps)
    step = max(1, (f1 - f0) // max(1, ncols * 2))
    tiles = []
    for f in range(f0, f1 + 1, step):
        cap.set(cv2.CAP_PROP_POS_FRAMES, f)
        ok, fr = cap.read()
        if not ok:
            continue
        crop = fr[win[1]:win[3], win[0]:win[2]]
        if crop.size == 0:
            continue
        big = cv2.resize(crop, None, fx=zoom, fy=zoom,
                         interpolation=cv2.INTER_LANCZOS4)
        cv2.ellipse(big, (int((cx - win[0]) * zoom), int((cy - win[1]) * zoom)),
                    (int(rx * zoom), int(ry * zoom)), 0, 0, 360, (0, 0, 255), 1)
        # 该帧的候选球块
        for (tt, bx, by, area) in cand["chain"]:
            if abs(tt - f / fps) > 0.5 / fps:
                continue
            px, py = int((bx - win[0]) * zoom), int((by - win[1]) * zoom)
            cv2.circle(big, (px, py), 8, (0, 255, 255), 2)
            relx = (bx - cx) / max(1.0, rx)
            cv2.putText(big, f"x{relx:+.2f}", (px + 6, py),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.45, (0, 255, 255), 1)
        cv2.putText(big, f"{f / fps:.2f}s", (4, 18),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.5, (255, 255, 255), 1)
        tiles.append(big)
    cap.release()
    if not tiles:
        return False
    # 排成两行
    per = max(1, len(tiles) // 2)
    rows = []
    for i in range(0, len(tiles), per):
        chunk = tiles[i:i + per]
        hh = max(x.shape[0] for x in chunk)
        chunk = [cv2.copyMakeBorder(x, 0, hh - x.shape[0], 0, 0,
                                    cv2.BORDER_CONSTANT, value=(0, 0, 0))
                 for x in chunk]
        rows.append(np.hstack(chunk))
    ww = max(r.shape[1] for r in rows)
    rows = [cv2.copyMakeBorder(r, 0, 0, 0, ww - r.shape[1],
                               cv2.BORDER_CONSTANT, value=(0, 0, 0)) for r in rows]
    sheet = np.vstack(rows)
    head = np.zeros((34, sheet.shape[1], 3), np.uint8)
    cv2.putText(head, f"cand t={cand['t0']:.2f}~{cand['t1']:.2f}s  "
                      f"ball rel-x at rim {cand['rel_x_at_rim']:+.2f}  "
                      f"drop {cand['drop_px']:.0f}px  "
                      f"（黄圈=候选球块，红圈=篮圈）",
                (6, 23), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (255, 255, 255), 1)
    out_png.parent.mkdir(parents=True, exist_ok=True)
    cv2.imwrite(str(out_png), np.vstack([head, sheet]))
    return True


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="篮下进球标注：扫候选 + 出证据图")
    ap.add_argument("--video", required=True)
    ap.add_argument("--out", default="out/label_baskets")
    ap.add_argument("--hoop-weights", default="runs/detect/rim/weights/best.pt")
    ap.add_argument("--device", default="0")
    ap.add_argument("--min-chain", type=int, default=3,
                    help="候选链最少几帧（越小候选越多，宁多勿漏）")
    ap.add_argument("--no-ui", action="store_true", help="只出证据图，不进入标注")
    argv = argv if argv is not None else sys.argv[1:]
    args = ap.parse_args(argv)

    from aihoop.hoopsight import SightConfig

    class _Cfg:
        pass
    cfg = _Cfg()
    cfg.sight = SightConfig()
    cfg.sight.stride = 1
    cfg.hoop_weights = args.hoop_weights
    cfg.device = args.device
    cfg.min_chain = args.min_chain

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    print(f"[1/3] 扫候选：{args.video}")
    cands, meta = scan_candidates(args.video, cfg)
    print(f"      篮筐 {meta['hoop']}，窗口 {meta['window']}，"
          f"球块采样 {meta['blob_samples']} 个 → 候选 {len(cands)} 个")
    (out / "candidates.json").write_text(
        json.dumps({"meta": meta, "candidates": cands}, ensure_ascii=False,
                   indent=1), encoding="utf-8")

    print("[2/3] 出证据图 …")
    sheets = []
    for i, c in enumerate(cands):
        p = out / "sheets" / f"cand_{i:03d}_t{c['t0']:07.2f}.png"
        if make_sheet(args.video, c, p):
            c["sheet"] = str(p)
            sheets.append(p)
    print(f"      出了 {len(sheets)} 张证据图 → {out / 'sheets'}")

    print("[3/3] 标注")
    if args.no_ui:
        print("      （--no-ui）跳过交互；请打开 sheets/ 里的图，"
              "把判断填进 labels/basket_labels.jsonl")
        _write_template(out, cands)
        return 0
    return _interactive_label(out, cands, args.video)


def _write_template(out: Path, cands: list) -> None:
    """写一份空标注模板，方便无界面时手填。"""
    lab = out / "labels"
    lab.mkdir(parents=True, exist_ok=True)
    p = lab / "basket_labels.jsonl"
    if p.exists():
        return
    with open(p, "w", encoding="utf-8") as f:
        for i, c in enumerate(cands):
            f.write(json.dumps({"idx": i, "t0": c["t0"], "t1": c["t1"],
                                "label": "", "note": ""},
                               ensure_ascii=False) + "\n")
    print(f"      已写标注模板：{p}（label 填 made / miss / unclear）")


def _interactive_label(out: Path, cands: list, video: str) -> int:
    """交互标注：一张一张看证据图，键盘判进球。

    键位：
      y / 1  进球        n / 0  没进
      u / 2  看不清      b      回上一个
      s      保存并退出  q      不保存退出
    """
    cv2, _np = _require_cv()
    lab = out / "labels"
    lab.mkdir(parents=True, exist_ok=True)
    labfile = lab / "basket_labels.jsonl"
    labels: dict[int, dict] = {}
    if labfile.exists():
        for line in labfile.read_text(encoding="utf-8").splitlines():
            if line.strip():
                d = json.loads(line)
                labels[int(d["idx"])] = d
    print("\n键位： y=进球  n=没进  u=看不清  b=上一个  s=保存退出  q=放弃退出")
    i = 0
    while 0 <= i < len(cands):
        c = cands[i]
        prev_lab = labels.get(i, {}).get("label", "")
        img = None
        if c.get("sheet") and Path(c["sheet"]).exists():
            img = cv2.imread(c["sheet"])
        if img is None:
            i += 1
            continue
        show = img.copy()
        cv2.putText(show, f"[{i + 1}/{len(cands)}] "
                          f"y=进球 n=没进 u=看不清 b=上一个 s=保存 q=退出"
                          f"   当前标注: {prev_lab or '（未标）'}",
                    (8, show.shape[0] - 10), cv2.FONT_HERSHEY_SIMPLEX, 0.7,
                    (0, 255, 0), 2)
        cv2.imshow("basket label", show)
        k = cv2.waitKey(0) & 0xFF
        if k in (ord('y'), ord('1')):
            labels[i] = {"idx": i, "t0": c["t0"], "t1": c["t1"],
                         "label": "made", "sheet": c.get("sheet", "")}
            i += 1
        elif k in (ord('n'), ord('0')):
            labels[i] = {"idx": i, "t0": c["t0"], "t1": c["t1"],
                         "label": "miss", "sheet": c.get("sheet", "")}
            i += 1
        elif k in (ord('u'), ord('2')):
            labels[i] = {"idx": i, "t0": c["t0"], "t1": c["t1"],
                         "label": "unclear", "sheet": c.get("sheet", "")}
            i += 1
        elif k == ord('b'):
            i = max(0, i - 1)
        elif k == ord('s'):
            break
        elif k == ord('q'):
            cv2.destroyAllWindows()
            print("已放弃保存")
            return 1
    cv2.destroyAllWindows()
    with open(labfile, "w", encoding="utf-8") as f:
        for k in sorted(labels):
            f.write(json.dumps(labels[k], ensure_ascii=False) + "\n")
    made = sum(1 for v in labels.values() if v["label"] == "made")
    miss = sum(1 for v in labels.values() if v["label"] == "miss")
    unc = sum(1 for v in labels.values() if v["label"] == "unclear")
    print(f"已保存 {len(labels)} 条标注 → {labfile}")
    print(f"  进球 {made} / 没进 {miss} / 看不清 {unc}")
    print("下一步：python scripts\\tune_basket_rules.py --video "
          f"{video} --labels {labfile}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
