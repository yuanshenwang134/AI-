# -*- coding: utf-8 -*-
"""把一份 `raw_track.json` 里的**旧引擎逐帧依据**摘要成可核对的事实清单。

用途有两个：
  1. 讲清楚"逐帧依据里到底存了什么"——旧记录每帧只存**被选中的那一个筐**
     （`frames[].rim`），**没有候选列表**，所以"候选里有没有真筐"这种问题是回答不了的；
  2. schema 2 已补上"全部候选 + 实际跟踪筐 + 选择/拒绝原因"，用同一个脚本对比新旧记录，
     确认新字段真的落盘了。

输出：
  * 逐帧记录含哪些键、`rim` 字段长什么样；
  * 判罚时刻用的筐（几条、各自的 cx）；
  * 逐帧被选中的筐按时间分成多少段、每段的位置范围（>--min-dur 秒的段）；
  * 段数与判罚时刻位置的关系（避免把"逐帧位置"说成"判罚用的筐"）。

用法：python scripts/legacy_trace_summary.py <raw_track.json>
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("raw")
    ap.add_argument("--min-dur", type=float, default=0.3, help="只列出长于该秒数的稳定段")
    ap.add_argument("--jump", type=float, default=30.0, help="相邻帧筐心位移超过该值就算新段")
    args = ap.parse_args()

    data = json.loads(Path(args.raw).read_text(encoding="utf-8"))
    trace = (data.get("detections_meta") or {}).get("legacy_shots")
    if not trace:
        print("这个任务没有 legacy_shots 逐帧依据")
        return 2
    frames = trace.get("frames") or []
    events = trace.get("events") or []

    with_rim = [f for f in frames if f.get("rim")]
    print("逐帧 %d 帧；其中有筐 %d 帧；%d 帧记录键 = %s"
          % (len(frames), len(with_rim), len(frames), sorted(frames[0].keys()) if frames else "-"))
    if with_rim:
        r = with_rim[0]["rim"]
        print("  frames[].rim 结构：%s" % json.dumps(r, ensure_ascii=False))
        if all('rim_candidates' in f and 'tracked_rim' in f and 'tracker_reason' in f for f in frames):
            candidates=[c for f in frames for c in f['rim_candidates']]
            print("  trace v2：完整记录 %d 帧、%d 个检测器候选；有实际跟踪筐与选择/拒绝原因。" % (len(frames),len(candidates)))
            print("  候选记录键：", sorted(candidates[0]) if candidates else [])
            print("  跟踪摘要：", json.dumps(trace.get('rim_tracking'),ensure_ascii=False))
        else:
            print("  旧/不完整记录：不能验证全部候选；rim 只是送入跟踪器的框，不是实际跟踪筐。")

    print("\n事件时刻实际跟踪筐（旧记录不可恢复，显示 None；%d 条）：" % len(events))
    by_frame = {int(f["frame"]): f for f in frames}
    jud_cx = []
    for ev in events:
        f = by_frame.get(int(ev.get("frame") or -1)) or {}
        box = (f.get("rim") or {}).get("xyxy")
        selected_cx = round((box[0] + box[2]) / 2, 1) if box else None
        tracked=f.get('tracked_rim')
        # 重新确认当天帧的 unknown 可能是旧证据段的收尾，不能套新筐位置。
        segment=f.get('evidence_segment')
        if tracked and segment!=tracked.get('segment'):
            transitions=(trace.get('rim_tracking') or {}).get('transitions') or []
            tracked=next((r.get('previous') for r in transitions if r.get('frame')==f.get('frame') and (r.get('previous') or {}).get('segment')==segment),None)
        cx=round(tracked['cx'],1) if tracked else None
        print("     selected_cx=%s tracker_reason=%s evidence_segment=%s" % (selected_cx,f.get('tracker_reason'),segment))
        jud_cx.append(cx)
        print("   t=%-7s %-9s 筐 cx=%s cross=%s"
              % (ev.get("t"), ev.get("type"), cx, ev.get("crossing_t")))

    eps = []
    for f in frames:
        box = (f.get("rim") or {}).get("xyxy")
        if not box:
            continue
        t = f["t"]
        cx = (box[0] + box[2]) / 2
        cy = (box[1] + box[3]) / 2
        w = box[2] - box[0]
        if not eps or abs(cx - eps[-1]["cx"]) > args.jump or (t - eps[-1]["t1"]) > 0.5:
            eps.append(dict(t0=t, t1=t, cx=cx, cy=cy, w=w, n=1, xs=[cx]))
        else:
            e = eps[-1]
            e.update(t1=t, cx=cx, cy=cy, w=w, n=e["n"] + 1)
            e["xs"].append(cx)
    big = [e for e in eps if e["t1"] - e["t0"] >= args.min_dur]
    print("\n逐帧被选中的筐共 %d 段，其中 ≥%.1fs 的 %d 段：" % (len(eps), args.min_dur, len(big)))
    for e in big:
        has = [ev["type"] for ev in events
               if e["t0"] <= (by_frame.get(int(ev.get("frame") or -1)) or {}).get("t", -1) <= e["t1"]]
        print("   %6.2f~%6.2f %5.2fs %4d帧  cx=%.0f~%.0f  cy≈%.0f  w≈%.0f  同段判罚=%s"
              % (e["t0"], e["t1"], e["t1"] - e["t0"], e["n"],
                 min(e["xs"]), max(e["xs"]), e["cy"], e["w"], has or "-"))
    print("\n结论口径提醒：上面这些是**逐帧被选中的筐**在不同时间段的位置，"
          "只有其中 %d 个出现在判罚时刻；不要把它们都称为“判罚用的筐”。"
          % len([c for c in jud_cx if c is not None]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
