"""Replay six recorded selected-ball/hoop streams; no detector inference."""
import argparse
import json
from pathlib import Path
from aihoop.legacy_shots.stream import replay_trace

CASES=[('game01','5c3f0020f46d',1),('fixedcam','b2aa966c9bb6',0),
       ('dairy','643047b9ec79',0),('day','e9513b6cae7c',0),
       ('night','7b59a3218687',0),('nathan','44ea917d92fb',0)]
def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--root',type=Path,default=Path(__file__).resolve().parents[1])
    p.add_argument('--out',type=Path,required=True)
    args=p.parse_args(); results=[]
    for name,jid,expected in CASES:
        trace=json.loads((args.root/'out'/jid/'raw_track.json').read_text(encoding='utf8'))['detections_meta']['legacy_shots']
        old=trace['events'];new=replay_trace(trace)
        assert len(old)==len(new),(name,'attempt count')
        allowed={'suggested_made','review_t','review_reason','evidence_grade'}
        for a,b in zip(old,new):
            assert all(b.get(k)==v for k,v in a.items() if k not in allowed),(name,'event changed')
        suggestions=[e for e in new if e.get('suggested_made') is True]
        assert len(suggestions)==expected,(name,'suggestion count')
        for e in suggestions:
            assert e['type']=='uncertain' and e['made'] is None and e['points']==0
            assert e['crossing_t'] is None and abs(e['review_t']-16.483)<.001
        assert sum(e['points'] for e in old)==sum(e['points'] for e in new)
        results.append(dict(name=name,job=jid,before=old,after=new))
        print(name,'attempts',len(new),'makes',sum(e['type']=='make' for e in new),'suggestions',len(suggestions))
    args.out.parent.mkdir(parents=True,exist_ok=True)
    args.out.write_text(json.dumps(results,ensure_ascii=False,indent=2),encoding='utf8')
if __name__=='__main__':main()
