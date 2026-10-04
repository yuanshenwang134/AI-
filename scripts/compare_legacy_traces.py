# -*- coding: utf-8 -*-
"""比较两份 `raw_track.json` 里**旧引擎的出手事件**是否逐字段一致。

用途：改了判定代码（例如加逐帧诊断、证据分段）之后，最需要回答的问题是
"在同一份已经录好的输入上，判定结果有没有被改动"。这个脚本不重跑视频、
不碰 GPU，只把两份产物里 `detections_meta.legacy_shots.events` 拉出来逐个字段比。

用法：python scripts/compare_legacy_traces.py <a/raw_track.json> <b/raw_track.json>
"""
from __future__ import annotations

import json
import sys
from pathlib import Path


def load_events(p: Path) -> list[dict]:
    data = json.loads(p.read_text(encoding="utf-8"))
    ls = (data.get("detections_meta") or {}).get("legacy_shots") or {}
    return ls.get("events") or []


def main() -> int:
    if len(sys.argv) < 3:
        print(__doc__)
        return 2
    a, b = Path(sys.argv[1]), Path(sys.argv[2])
    ea, eb = load_events(a), load_events(b)
    print("A = %s（%d 条事件）" % (a, len(ea)))
    print("B = %s（%d 条事件）" % (b, len(eb)))
    if len(ea) != len(eb):
        print("✗ 事件条数不同 → 判定被改动，先看条数差在哪")
    # 只看两边都有的键（新增字段不算"改动"）
    keys = sorted(set().union(*[set(e) for e in ea + eb])) if (ea or eb) else []
    diffs = 0
    for i in range(min(len(ea), len(eb))):
        for k in keys:
            if k not in ea[i] or k not in eb[i]:
                continue
            if ea[i][k] != eb[i][k]:
                diffs += 1
                print("   ✗ #%d 字段 %s: A=%r  B=%r" % (i, k, ea[i][k], eb[i][k]))
    only_a = sorted(set().union(*[set(e) for e in ea]) - set().union(*[set(e) for e in eb])) if ea else []
    only_b = sorted(set().union(*[set(e) for e in eb]) - set().union(*[set(e) for e in ea])) if eb else []
    if only_a:
        print("   只有 A 有的字段：%s" % only_a)
    if only_b:
        print("   只有 B 有的字段（新增，不算改动）：%s" % only_b)
    if not diffs and len(ea) == len(eb):
        print("✓ 共有字段逐条一致（%d 条事件）" % len(ea))
    return 0 if (not diffs and len(ea) == len(eb)) else 1


if __name__ == "__main__":
    raise SystemExit(main())
