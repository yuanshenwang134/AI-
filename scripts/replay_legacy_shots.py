"""重放新版 raw_track 内的旧引擎逐帧依据；不需要 GPU，不改历史任务。"""
import argparse
import json
import sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src'))
from aihoop.legacy_shots.stream import replay_trace

def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--raw',required=True)
    parser.add_argument('--out',required=True)
    args=parser.parse_args()
    raw=Path(args.raw).resolve();out=Path(args.out).resolve()
    if raw==out:parser.error('输出不能覆盖输入 raw_track')
    data=json.loads(raw.read_text(encoding='utf-8'))
    trace=data.get('detections_meta',{}).get('legacy_shots')
    if not trace:parser.error('此任务没有 legacy_shots 逐帧依据；需要使用新接入引擎生成新任务')
    events=replay_trace(trace)
    result={'raw':str(raw),'model_sha256':trace.get('model_sha256'),
            'exact_match':events==trace['events'],'events':events}
    out.parent.mkdir(parents=True,exist_ok=True)
    out.write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
    print(f"events={len(events)}, exact_match={result['exact_match']}")
    return 0 if result['exact_match'] else 1
if __name__=='__main__':raise SystemExit(main())
