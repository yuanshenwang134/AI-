# -*- coding: utf-8 -*-
"""核对一份 `raw_track.json` 里**篮筐跟踪计数口径**是否自洽（真机可复现的验收集）。

为什么单独有这个脚本：用户明确要求"拒绝候选数"与"涉及帧数"必须分开统计 ——
同一帧拒掉 2 个候选只能算 1 个拒绝帧。合成单测能钉住这一点，但真机上是否成立
（检测器到底会不会给出同帧多候选）必须用真产物核。

检查项：
  1. 摘要 `rejected_candidates` == 逐帧 `center_rejected_candidates` 之和；
  2. 摘要 `rejected_frames` == 有拒绝（>0）的帧数（我自己重新数一遍）；
  3. 报告同帧被拒 >=2 个候选的帧数（这些帧在帧计数里只算 1）；
  4. 报告 `selection_reason` / `tracker_reason` 的取值分布，并标出越界取值；
  5. 报告跟踪段重建记录（transitions）与片尾跟踪状态。

用法：python scripts/verify_rim_trace_counters.py out/<job>/raw_track.json
"""
from __future__ import annotations

import argparse
import collections
import json
from pathlib import Path

SEL_OK = {"selected", "low_confidence", "invalid_shape", "not_nearest_hint",
          "outside_center_lock", "not_selected", "manual_override"}
TRK_OK = {"pending_confirmation", "initialized", "tracked", "jump_rejected", "identity_wait",
          "scale_rejected", "no_candidate", "reacquired_near", "reacquired_far",
          "uninitialized", "not_submitted"}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("raw")
    args = ap.parse_args()
    data = json.loads(Path(args.raw).read_text(encoding="utf-8"))
    ls = (data.get("detections_meta") or {}).get("legacy_shots") or {}
    frames, summary = ls.get("frames") or [], ls.get("rim_tracking") or {}
    if not summary:
        print("这份产物没有 rim_tracking（旧 trace 或不是旧引擎路径），无法核对")
        return 2

    per = [f.get("center_rejected_candidates", 0) for f in frames]
    frames_with = sum(1 for x in per if x > 0)
    print("lock_requested=%s lock_enabled=%s hint=%s lock_width=%s"
          % (summary.get("lock_requested"), summary.get("lock_enabled"),
             summary.get("center_hint"), summary.get("lock_width")))
    ok1 = sum(per) == summary.get("rejected_candidates")
    ok2 = frames_with == summary.get("rejected_frames")
    print("1) 候选数：逐帧合计 %d vs 摘要 %s → %s"
          % (sum(per), summary.get("rejected_candidates"), "一致" if ok1 else "**不一致**"))
    print("2) 帧数  ：有拒绝的帧 %d vs 摘要 %s → %s"
          % (frames_with, summary.get("rejected_frames"), "一致" if ok2 else "**不一致**"))
    multi = [f for f in frames if f.get("center_rejected_candidates", 0) >= 2]
    print("3) 同帧被拒 >=2 个候选的帧 %d（在帧计数里各只算 1 帧）" % len(multi))

    sels = collections.Counter(c.get("selection_reason") for f in frames
                               for c in (f.get("rim_candidates") or []))
    trks = collections.Counter(f.get("tracker_reason") for f in frames)
    print("4) selection_reason：%s" % dict(sels))
    print("   越界取值：%s" % (sorted(set(sels) - SEL_OK) or "无"))
    print("   tracker_reason  ：%s" % dict(trks))
    print("   越界取值：%s" % (sorted(set(trks) - TRK_OK) or "无"))

    print("5) 跟踪段重建 %d 条：" % len(summary.get("transitions") or []))
    for t in summary.get("transitions") or []:
        prev, cur = t.get("previous") or {}, t.get("current") or {}
        print("      t=%-7.2f %-16s 段 %s→%s  cx %s→%s  收尾未决=%s"
              % (t.get("t"), t.get("reason"), prev.get("segment"), cur.get("segment"),
                 round(prev["cx"]) if prev.get("cx") else None,
                 round(cur["cx"]) if cur.get("cx") else None, t.get("closed_attempts")))
    print("   片尾跟踪：%s" % json.dumps(summary.get("last_tracked"), ensure_ascii=False))
    return 0 if (ok1 and ok2) else 1


if __name__ == "__main__":
    raise SystemExit(main())
