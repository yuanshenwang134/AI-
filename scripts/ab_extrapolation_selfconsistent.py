#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""自洽 A/B：`HoopConfig.allow_extrapolated_crossing` 关 / 开（nathan 罚球素材）。

**为什么要"自洽"**：判断外推开关收益时，球轨迹与篮筐轨迹必须来自**同一次运行**。
之前的离线对照（`_tmp/probe_nathan_crossings.py`）把「某次诊断导出的球轨迹 dump」和
「另一次评测产出的 raw_track.json 里的筐采样」配在一起 —— dump 里的筐中值是
(553,154,31,10)，那次 job 的是 (551.8,153.5,30.6,9.5)，两次不同运行的东西拼在一起
在方法上不成立（队友侧提出过这一点）。本脚本跑一次真实管线，球轨迹与筐轨迹都取自
这一次运行。

**结论（2026-09-27 实测，nathan_freethrow.mov）**：
  * 默认（外推关）：3 个 `cross_measured` 命中；2.64s 那次是 `cross_rim_contact`
    （人工确认真值＝不中，偏移 +0.63×rx）；另有 3 条未知。
  * 打开外推：同样 3 个 `cross_measured`，**多出 19.36s → `cross_extrapolated`**
    （穿越 21.141s，横向偏移 −0.01×rx）。按产品分级它只是 `suggested_made`（建议），
    **不计入已确认命中**。
  * 开关**不会**让 2.64s 那次假进球复活（仍是 `cross_rim_contact`）。
  → 开关的收益是"把一条没有证据的未知升级成带建议的未知"，不是"多召回一个进球"。

用法（需要 GPU；不需要后端）：

    python scripts/ab_extrapolation_selfconsistent.py
    python scripts/ab_extrapolation_selfconsistent.py --video nathan_freethrow.mov --device 0
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

from aihoop.court import Calibration                                     # noqa: E402
from aihoop.hoop import Hoop, HoopConfig, HoopTrack, detect_shots        # noqa: E402
from aihoop.sources import VideoSource                                   # noqa: E402

BALL = ROOT / "runs" / "detect" / "ball" / "weights" / "best.pt"
RIM = ROOT / "runs" / "detect" / "rim" / "weights" / "best.pt"


def samples_dir() -> Path:
    env = os.environ.get("AIHOOP_SAMPLES", "")
    return Path(env) if env else Path(r"D:\dsh_folder\aihoop_assets\samples")


def run_once(video: Path, device: str):
    cal = Calibration(name="unavailable", method="unavailable")
    src = VideoSource(str(video), cal, stride=1, player_stride=2,
                      detect_players=False, score_policy="auto",
                      visual_shot_value=2, auto_sliding=False,
                      ball_weights=str(BALL), hoop_weights=str(RIM),
                      device=device)
    rt = src.run(progress=lambda p, m="": None)
    meta = rt.detections_meta
    h = meta.get("hoop") or {}
    if not h.get("samples"):
        raise RuntimeError(f"这次运行没有筐样本（visual_error={meta.get('visual_error')}）")
    samples = [(s[0], Hoop(cx=s[1]["cx"], cy=s[1]["cy"], rx=s[1]["rx"],
                           ry=s[1]["ry"], board=s[1].get("board")))
               for s in h["samples"]]
    ht = HoopTrack(samples=samples, fps=h["fps"], duration=h["duration"],
                   votes=h["votes"], frames=h["frames"], width=960)
    tracks = getattr(src, "_tracks_px", []) or []
    return meta, ht, tracks


def report(tag: str, shots, ht: HoopTrack) -> None:
    print(f"\n=== {tag} → {len(shots)} 次出手")
    for s in shots:
        if s.cross_x is None or s.crossing_t is None:
            print(f"  t={s.t:>6.2f}  没判到穿筐  made={s.made}  evidence={s.evidence!r}")
            continue
        hc = ht.at(s.crossing_t)
        got = s.cross_x - hc.cx
        mark = ("在净空门槛 0.467 之内" if abs(got) <= hc.rx * 0.467
                else "超出净空门槛 0.467")
        print(f"  t={s.t:>6.2f}  穿越 {s.crossing_t:>7.3f}s  made={str(s.made):>5}  "
              f"{s.evidence:<20} 筐心={hc.cx:6.1f} rx={hc.rx:5.1f}  "
              f"偏移={got:+6.1f}px = {got / hc.rx:+.2f} rx   ← {mark}")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--video", default="nathan_freethrow.mov")
    ap.add_argument("--device", default="0")
    args = ap.parse_args(argv)

    video = samples_dir() / args.video
    if not video.exists():
        print(f"[error] 找不到素材：{video}（用 --video 或设 AIHOOP_SAMPLES）")
        return 2

    meta, ht, tracks = run_once(video, args.device)
    print(f"素材：{video.name}")
    print(f"篮筐中值：{(meta.get('hoop') or {}).get('median')}")
    print(f"筐稳健漂移 {(meta.get('hoop') or {}).get('robust_drift_px')}；"
          f"镜头 {(meta.get('camera_motion') or {}).get('verdict')}")
    print(f"球轨迹 {len(tracks)} 条（同一次运行）")

    for tag, flag in (("默认（外推关）", False), ("打开外推", True)):
        cfg = HoopConfig()
        cfg.allow_extrapolated_crossing = flag
        report(tag, detect_shots(tracks, ht, cfg), ht)
    print("\n提醒：`cross_extrapolated` 在产品分级里只是**建议**（suggested_made），"
          "不计入已确认命中。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
