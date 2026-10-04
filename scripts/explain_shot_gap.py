"""Read saved frame diagnostics around a disputed shot; never runs detection."""
import argparse
import json
from collections import Counter
from pathlib import Path


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--raw',type=Path,required=True)
    parser.add_argument('--at',type=float,required=True)
    parser.add_argument('--span',type=float,default=2)
    args=parser.parse_args()
    raw=json.loads(args.raw.read_text(encoding='utf-8'))
    trace=raw.get('detections_meta',{}).get('hoopsight',{}).get('candidate_trace')
    if not trace:
        parser.exit(2,'该产物没有逐帧诊断。需由复检方用新版重新分析，旧结果不能补造原因。\n')
    frames=[r for r in trace['frames'] if abs(r['t']-args.at)<=args.span]
    spans=[r for r in trace.get('spans',[]) if r['end']>=args.at-args.span and r['start']<=args.at+args.span]
    print(json.dumps({'interval':[args.at-args.span,args.at+args.span],
                      'stage_counts':dict(Counter(r['stage'] for r in frames)),
                      'frames':frames,'spans':spans,
                      'truncated':trace.get('truncated',0),
                      'spans_truncated':trace.get('spans_truncated',0)},ensure_ascii=False,indent=2))


if __name__=='__main__':
    main()
