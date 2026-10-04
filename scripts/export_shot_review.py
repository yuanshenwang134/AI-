"""Export review CSV and optional clips from existing raw_track.json; no inference.

Example: python scripts/export_shot_review.py --raw out/JOB/raw_track.json --out out/JOB/review
Add --clips to cut candidates with ffmpeg. Add missed shots to truth.csv manually.
Truth times must be visible release times, not scoreboard change times.
"""
import argparse
import csv
import json
import math
from pathlib import Path
import shutil
import subprocess


def review_rows(raw, before=2.0, after=4.0):
    duration = float(raw.get('duration') or 0)
    rows = []
    for i, shot in enumerate(raw.get('attempts', [])):
        t = float(shot['t'])
        if not math.isfinite(t) or t < 0:
            raise ValueError('Invalid candidate timestamp')
        crossing = shot.get('crossing_t')
        crossing = float(crossing) if crossing is not None else t
        if not math.isfinite(crossing) or crossing < 0:
            crossing = t
        rows.append(dict(candidate_id=i+1, candidate_t=t,
                         clip_start=max(0,t-before),
                         clip_end=min(duration,max(t,crossing)+after) if duration > 0 else max(t,crossing)+after,
                         model_result='make' if shot.get('made') is True else
                         'miss' if shot.get('made') is False else 'unknown',
                         source=shot.get('source',''),
                         evidence=shot.get('evidence',''), crossing_t=shot.get('crossing_t',''),
                         suggested_made=shot.get('suggested_made',''),
                         needs_review='needs_review' in (shot.get('tags') or []),
                         decision='', truth_id='', release_t='', result='', note=''))
    return rows


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--raw',type=Path,required=True)
    parser.add_argument('--out',type=Path,required=True)
    parser.add_argument('--video',type=Path)
    parser.add_argument('--clips',action='store_true')
    args=parser.parse_args()
    raw=json.loads(args.raw.read_text(encoding='utf-8'))
    rows=review_rows(raw)
    # A new destination prevents overwriting someone's annotations.
    if args.out.exists():
        parser.error('输出目录已存在；请选择新目录，避免覆盖人工标注。')
    video=args.video or Path(raw.get('video_path') or '')
    if args.clips and (not video.is_file() or not shutil.which('ffmpeg')):
        parser.error('生成短片需要可读的视频文件与 ffmpeg。')
    args.out.mkdir(parents=True)
    fields=['candidate_id','candidate_t','clip_start','clip_end','model_result','source',
            'evidence','crossing_t','suggested_made','needs_review',
            'decision','truth_id','release_t','result','note']
    with (args.out/'candidates.csv').open('w',newline='',encoding='utf-8-sig') as stream:
        writer=csv.DictWriter(stream,fieldnames=fields);writer.writeheader();writer.writerows(rows)
    (args.out/'truth.csv').write_text('truth_id,release_t,result,note\n',encoding='utf-8-sig')
    (args.out/'README.txt').write_text(
        '候选表不是完整真值。请另看完整原视频，补录漏掉的投篮。\n'
        'candidates.csv: decision 填 shot / duplicate / not_shot / unclear；同一投用相同 truth_id。\n'
        'truth.csv: 每次真实出手只填一行；release_t 为出手秒数，result 为 make / miss / unknown。\n'
        '模型结果只是参考，不要复制它作为人工真值；看不清就填 unknown。\n'
        '短片时间加 clip_start 才是原视频时间；出手时间不应填写为入筐或比分变化时间。\n'
        '必须复核完整视频才能测漏检，只有候选短片无法证明召回率。\n',encoding='utf-8')
    if args.clips:
        for row in rows:
            if row['clip_end'] <= row['clip_start']:
                continue
            subprocess.run(['ffmpeg','-nostdin','-v','error','-n','-ss',str(row['clip_start']),
                            '-i',str(video),'-t',str(row['clip_end']-row['clip_start']),
                            '-map','0:v:0','-map','0:a?','-c:v','libx264','-c:a','aac',
                            str(args.out/f"candidate_{row['candidate_id']:03d}.mp4")],check=True)
    print(f'已导出 {len(rows)} 个候选；请通看原视频补齐漏检真值：{args.out}')


if __name__=='__main__':
    main()
