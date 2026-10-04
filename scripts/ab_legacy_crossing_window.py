"""Same-trace A/B for the existing opt-in measured-crossing window."""
import argparse
import copy
import json
from pathlib import Path
from aihoop.legacy_shots.stream import replay_trace

def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--manifest',type=Path,required=True)
    parser.add_argument('--out',type=Path,required=True)
    args=parser.parse_args();root=Path(__file__).resolve().parents[1];results=[]
    for case in json.loads(args.manifest.read_text(encoding='utf8')):
        trace=json.loads((root/'out'/case['job']/'raw_track.json').read_text(encoding='utf8'))['detections_meta']['legacy_shots']
        off=copy.deepcopy(trace);off['config']['score']['make_window_from_crossing']=False
        on=copy.deepcopy(trace);on['config']['score']['make_window_from_crossing']=True
        before=replay_trace(off);after=replay_trace(on)
        results.append(dict(name=case['tag'],job=case['job'],before=before,after=after,exact_match=before==after))
        print(case['tag'],'off/on makes',sum(e['type']=='make' for e in before),sum(e['type']=='make' for e in after),'exact',before==after)
    args.out.parent.mkdir(parents=True,exist_ok=True)
    args.out.write_text(json.dumps(results,ensure_ascii=False,indent=2),encoding='utf8')
if __name__=='__main__':main()
