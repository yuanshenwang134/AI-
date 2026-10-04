#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""用「有人工真值的素材」跑一遍投篮判定，把结果和真值并排打出来。

为什么要这个脚本（而不是只看报告页）：
  报告页只能告诉你「这次判出了几次出手」，没法告诉你**判错了没有**。
  评测必须有独立真值 —— 本仓库的真值放在 eval/ground_truth/*.ordinal.json，
  格式与旧项目一致：只记「第几球、进没进」，**没有人提供时间**。
  所以这里的比对规则很克制：
    * 保留人工顺序与模型时间，供后续人工核对；
    * 没有人工时间戳时只比较数量，个数一致也不硬对齐；
    * 真值里的 unknown 不计入分母（看不清的球不该算模型错）。

用法（需要后端已经在跑）：
  python scripts/eval_shots_vs_truth.py --samples <素材目录>
  python scripts/eval_shots_vs_truth.py --samples <目录> --only nathan --device 0

这是**开发素材上的自检**，不是产品准确率承诺：素材来自公开视频，
数量少、机位单一，报告里必须写清这一点。
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _post(base: str, path: str, body: dict) -> dict:
    req = urllib.request.Request(
        base + path, data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read().decode("utf-8"))


def _get(base: str, path: str) -> dict:
    with urllib.request.urlopen(base + path, timeout=60) as r:
        return json.loads(r.read().decode("utf-8"))


def load_truth(truth_dir: Path) -> dict:
    """读旧格式的真值文件：{视频文件名: [结果, ...]}。"""
    out = {}
    for p in sorted(truth_dir.glob("*.ordinal.json")):
        try:
            d = json.loads(p.read_text(encoding="utf-8"))
        except Exception as e:  # noqa: BLE001
            print(f"[warn] 真值读不了：{p.name}: {e}")
            continue
        shots = d.get("shots") or []
        out[d.get("video") or p.name] = {
            "results": [str(s.get("result", "unknown")) for s in shots],
            "note": d.get("source") or d.get("notes") or "",
            "path": str(p),
        }
    return out


def find_video(samples: Path, name: str) -> Path | None:
    p = samples / name
    if p.exists():
        return p
    stem = Path(name).stem
    for cand in sorted(samples.iterdir()):
        if cand.is_file() and cand.stem == stem:
            return cand
    return None


def run_one(base: str, video: Path, device: str, timeout_s: int) -> dict:
    body = {
        "source": "video",
        "video_path": str(video),
        # 只验证投篮判定本身：不跑球员 YOLO、不看比分牌、不要球场坐标。
        "detect_players": False,
        "score_policy": "court",
        "allow_no_calibration": True,
        "make_highlights": False,
        "stride": 1,
        "device": device,
    }
    t0 = time.time()
    jid = _post(base, "/api/jobs", body)["job_id"]
    last = ""
    while True:
        st = _get(base, f"/api/jobs/{jid}")
        msg = f"{st.get('status')} {st.get('progress', 0):.0%} {st.get('message', '')}"
        if msg != last:
            print(f"    {msg}", flush=True)
            last = msg
        if st.get("status") in ("done", "error"):
            break
        if time.time() - t0 > timeout_s:
            raise RuntimeError(f"{video.name} 超过 {timeout_s}s 还没跑完")
        time.sleep(5)
    rec = {"job_id": jid, "status": st.get("status"),
           "error": st.get("error"), "elapsed_s": round(time.time() - t0, 1),
           "attempts": []}
    raw = ROOT / "out" / jid / "raw_track.json"
    if raw.exists():
        rt = json.loads(raw.read_text(encoding="utf-8"))
        meta = rt.get("detections_meta") or {}
        rec["visual"] = meta.get("visual")
        rec["visual_error"] = meta.get("visual_error")
        rec["hoopsight_skipped"] = meta.get("hoopsight_skipped")
        rec["attempts"] = [
            {"t": a.get("t"), "made": a.get("made"), "conf": a.get("conf"),
             "source": a.get("source"), "tags": a.get("tags")}
            for a in rt.get("attempts", [])
        ]
    game = ROOT / "out" / jid / "game.json"
    if game.exists():
        rec["judgement"] = (json.loads(game.read_text(encoding="utf-8"))
                            .get("judgement"))
    return rec


def summarize(truth: list, attempts: list) -> dict:
    model = ["make" if a["made"] else ("miss" if a["made"] is False else "unknown")
             for a in attempts]
    known = [(i, t) for i, t in enumerate(truth) if t != "unknown"]
    res = {
        "truth_n": len(truth), "truth_make": sum(1 for x in truth if x == "make"),
        "model_n": len(model), "model_make": sum(1 for x in model if x == "make"),
        "model_unknown": sum(1 for x in model if x == "unknown"),
        "aligned": False, "per_shot": None,
    }
    # Equal counts do not establish identity: a missed shot plus a duplicate
    # can cancel out. Ordinal-only truth never authorizes per-shot accuracy.
    res["alignment_reason"] = "缺少人工时间戳；即使总数相等也不能确认逐球对应"
    res["truth_known"] = len(known)
    return res


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://127.0.0.1:8000")
    ap.add_argument("--samples", default="", help="素材目录（默认 env AIHOOP_SAMPLES）")
    ap.add_argument("--truth", default=str(ROOT / "eval" / "ground_truth"))
    ap.add_argument("--device", default="0")
    ap.add_argument("--only", default="", help="只跑文件名里含这个词的素材")
    ap.add_argument("--timeout", type=int, default=1800)
    ap.add_argument("--out", default=str(ROOT / "out" / "eval_shots_vs_truth.json"))
    args = ap.parse_args()

    import os
    samples = Path(args.samples or os.environ.get("AIHOOP_SAMPLES", ""))
    if not samples.exists():
        print(f"[error] 素材目录不存在：{samples}（用 --samples 或设 AIHOOP_SAMPLES）")
        return 2
    truth_dir = Path(args.truth)
    if not truth_dir.exists():
        print(f"[error] 真值目录不存在：{truth_dir}")
        return 2

    try:
        _get(args.base, "/api/health")
    except urllib.error.URLError as e:
        print(f"[error] 后端连不上（{args.base}）：{e}\n"
              "        先起后端： python -m uvicorn aihoop.api:app --port 8000")
        return 2

    truth = load_truth(truth_dir)
    report = []
    for name, t in truth.items():
        if args.only and args.only not in name:
            continue
        video = find_video(samples, name)
        if video is None:
            print(f"[skip] {name}：素材目录里没有这个文件")
            continue
        print(f"\n=== {name}")
        try:
            rec = run_one(args.base, video, args.device, args.timeout)
        except Exception as e:  # noqa: BLE001
            rec = {"status": "driver_error", "error": f"{type(e).__name__}: {e}",
                   "attempts": []}
        rec["video"] = name
        rec["truth"] = t["results"]
        rec["truth_note"] = t["note"]
        rec["cmp"] = summarize(t["results"], rec.get("attempts") or [])
        report.append(rec)

        c = rec["cmp"]
        print(f"    人工 {c['truth_n']} 球（进 {c['truth_make']}）：{t['results']}")
        print(f"    模型 {c['model_n']} 球（进 {c['model_make']}，未知 {c['model_unknown']}）："
              + str(["make" if a["made"] else ("miss" if a["made"] is False else "?")
                     for a in rec.get("attempts") or []]))
        if rec.get("error"):
            print(f"    错误：{str(rec['error'])[:300]}")
        if rec.get("visual_error"):
            print(f"    视觉路径放弃：{str(rec['visual_error'])[:160]}")
        if c["aligned"]:
            print(f"    逐球比对：{c['per_shot_ok']}/{c['per_shot_den']} 一致"
                  "（真值里 unknown 的不计分母）")
        else:
            print("    缺少人工时间戳 —— 不做逐球对齐，仅比较数量，不计算精确率或召回率")

    outp = Path(args.out)
    outp.parent.mkdir(parents=True, exist_ok=True)
    outp.write_text(json.dumps(report, ensure_ascii=False, indent=2),
                    encoding="utf-8")
    print(f"\n明细：{outp}")
    print("提醒：以上是开发素材上的自检结果，不能当作对外准确率承诺。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
