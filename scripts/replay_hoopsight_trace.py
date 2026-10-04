"""A/B replay of saved selected blobs, not full video inference or accuracy scoring."""
import argparse
from dataclasses import fields
import json
from pathlib import Path
import sys

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src'))
from aihoop.hoop import Hoop
from aihoop.hoopsight import SightConfig, _shots_from_blob_series


def replay(raw, tolerance):
    meta=raw.get('detections_meta',{}).get('hoopsight') or {}
    trace=meta.get('candidate_trace') or {}
    if not trace:
        return {'available':False,'reason':'no_saved_trace'}
    if trace.get('truncated'):
        return {'available':False,'reason':'frame_trace_truncated'}
    allowed={f.name for f in fields(SightConfig)}
    cfg=SightConfig(**{k:v for k,v in trace.get('config',{}).items() if k in allowed})
    cfg.exit_pixel_tolerance=tolerance
    cfg.min_confidence=meta.get('min_confidence',cfg.min_confidence)
    shots=[]
    for index in sorted({r['hoop_index'] for r in trace['frames']}):
        rows=[r for r in trace['frames'] if r['hoop_index']==index and r.get('selected')]
        if not rows:
            continue
        hp=meta['hoops'][index]
        hoop=Hoop(**{k:hp[k] for k in ('cx','cy','rx','ry')})
        lookup={r['t']:Hoop(cx=r['hoop'][0],cy=r['hoop'][1],rx=r['hoop'][2],ry=r['hoop'][3]) for r in rows}
        series=[(r['t'],*r['selected'][:3]) for r in rows]
        shots.extend(_shots_from_blob_series(series,hoop,cfg,
                      fps=trace.get('fps',raw['fps']),hoop_lookup=lambda t:lookup[t]))
    kept=[]
    for shot in sorted(shots,key=lambda s:(-s.confidence,s.t)):
        if shot.confidence >= cfg.min_confidence and not any(abs(shot.t-s.t)<cfg.min_shot_gap_s for s in kept):
            kept.append(shot)
    return {'available':True,'config_source':'saved' if trace.get('config') else 'current_defaults',
            'warning':'回放使用保存的时间与篮筐坐标，可能存在舍入；不评估检测器或逐球准确率。',
            'shots':[s.to_dict() for s in sorted(kept,key=lambda s:s.t)]}


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--raw',type=Path,nargs='+',required=True)
    parser.add_argument('--out',type=Path,required=True)
    args=parser.parse_args()
    result=[]
    for path in args.raw:
        raw=json.loads(path.read_text(encoding='utf-8'))
        result.append({'raw':str(path),'video':raw.get('video_path'),
                       'baseline':replay(raw,0),'candidate':replay(raw,.5)})
    args.out.parent.mkdir(parents=True,exist_ok=True)
    with args.out.open('x',encoding='utf-8') as stream:
        json.dump(result,stream,ensure_ascii=False,indent=2)
    for r in result:
        print(Path(r['video']).name, {key: [s['t'] for s in r[key].get('shots',[])]
               if r[key]['available'] else r[key]['reason'] for key in ('baseline','candidate')})


if __name__=='__main__':
    main()
