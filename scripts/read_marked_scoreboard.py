"""手动框选记分牌 → 用 Windows OCR 读出得分事件。

流程（实测可行）：
  1. 用户在画面上框出记分牌的**数字区域**（1 个或 2 个；两个分别是主/客队分），
     并给出起始比分；
  2. 每 step_s 秒抽一帧，把该区域**放大 4 倍**后存成 PNG
     （实测：放大后 Windows OCR 能稳定读出 "KPHS 27 AHS 35"；不放大就认不出）；
  3. 一次 PowerShell 调用**批量 OCR** 所有小图（避免逐张启动 PowerShell 的开销）；
  4. 从文字里抠出数字 → 用「比分只增不减 + 一次最多 +3」过滤误读 → 产出得分事件。

为什么不用模板匹配：现有 `scoreboard.py` 依赖样式模板，实测这段视频直接报
「画面里没有找到广播比分牌」。OCR 不挑样式，只要用户指一下区域。

用法：
    python scripts/read_marked_scoreboard.py --video data/new_video.mp4 `
        --box-home 402,62,438,92 --box-away 522,62,560,92 `
        --start 27,35 --step 0.5 --out out/sb_events.json
"""
from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import cv2  # noqa: E402


def grab_crops(video: str, boxes: dict, step_s: float, zoom: float,
               tmpdir: Path, max_seconds: float = 0.0) -> list[dict]:
    """按时间抽样，把每个区域放大后存成 PNG，返回 [{t, team, file}]。"""
    cap = cv2.VideoCapture(str(video))
    if not cap.isOpened():
        raise SystemExit(f"打不开视频：{video}")
    fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
    total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
    dur = total / fps if fps else 0.0
    if max_seconds and max_seconds > 0:
        dur = min(dur, max_seconds)
    step = max(1, int(round(step_s * fps)))
    items = []
    f = 0
    while f < total and (f / fps) <= dur:
        cap.set(cv2.CAP_PROP_POS_FRAMES, f)
        ok, fr = cap.read()
        if not ok:
            break
        t = f / fps
        for team, (x0, y0, x1, y1) in boxes.items():
            h, w = fr.shape[:2]
            a, b = max(0, int(x0)), max(0, int(y0))
            c, d = min(w, int(x1)), min(h, int(y1))
            crop = fr[b:d, a:c]
            if crop.size == 0:
                continue
            big = cv2.resize(crop, None, fx=zoom, fy=zoom,
                             interpolation=cv2.INTER_CUBIC)
            name = f"{int(round(t * 100)):07d}_{team}.png"
            cv2.imwrite(str(tmpdir / name), big)
            items.append({"t": round(t, 2), "team": team, "file": name})
        f += step
    cap.release()
    return items


def run_ocr(tmpdir: Path, out_json: Path) -> dict:
    ps = shutil.which("powershell") or shutil.which("powershell.exe") or \
        shutil.which("pwsh")
    if not ps:
        raise SystemExit("找不到 powershell，无法调用 Windows OCR")
    script = ROOT / "scripts" / "ocr_batch.ps1"
    cmd = [ps, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File",
           str(script), "-Dir", str(tmpdir), "-Out", str(out_json)]
    # 显式 utf-8 + errors=replace：Windows 上 text=True 默认 GBK，
    # PowerShell 输出里的非 GBK 字节会让它崩（实测 UnicodeDecodeError）
    r = subprocess.run(cmd, capture_output=True, text=True,
                       encoding="utf-8", errors="replace", timeout=1800)
    if r.returncode != 0:
        raise SystemExit(f"OCR 失败：{r.stderr[-500:] or r.stdout[-500:]}")
    return {d["file"]: (d.get("text") or "")
            for d in json.loads(out_json.read_text(encoding="utf-8"))}


from aihoop.score_text import parse_number, parse_scores, build_events

def locate_scoreboard_ocr(video, tmpdir, team_names=None):
    """Select proposals by readable score pairs across time, not static pixels."""
    from aihoop.score_text import parse_scores
    from aihoop.scoreboard import ScoreBugConfig, locate_score_bug_static, locate_score_bug_box
    cap = cv2.VideoCapture(str(video))
    width, height = int(cap.get(3)), int(cap.get(4))
    total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    proposals = []
    for locator in (locate_score_bug_static, locate_score_bug_box):
        try:
            bug = locator(str(video), ScoreBugConfig())
            if bug is not None:
                proposals.append((bug.x, bug.y, bug.w, bug.h))
        except Exception:
            pass
    # Overlapping generic edge bands. Do not bake in a particular video's box.
    for y0,y1 in ((0,.20),(.06,.22),(.78,1),(.86,1)):
        for x0,x1 in ((0,1),(0,.7),(.2,.8),(.3,1)):
            proposals.append((int(x0*width),int(y0*height),int((x1-x0)*width),int((y1-y0)*height)))
    proposals = list(dict.fromkeys(proposals))
    files = {i:[] for i in range(len(proposals))}
    for sample, frac in enumerate((.01,.25,.5,.75,.99)):
        cap.set(cv2.CAP_PROP_POS_FRAMES, max(0,int((total-1)*frac)))
        ok, frame = cap.read()
        if not ok: continue
        for i,(x,y,w,h) in enumerate(proposals):
            crop = frame[max(0,y):min(height,y+h),max(0,x):min(width,x+w)]
            if not crop.size:continue
            name=f'candidate_{i}_{sample}.png'
            cv2.imwrite(str(tmpdir/name),cv2.resize(crop,None,fx=3,fy=3,interpolation=cv2.INTER_CUBIC))
            files[i].append(name)
    cap.release()
    texts = run_ocr(tmpdir,tmpdir/'candidates.json')
    ranking=[]
    for i,names in files.items():
        parsed=[parse_scores(texts.get(n,''),team_names) for n in names]
        # Unlabelled numbers in a stand/clock are not enough to locate an overlay.
        hits=sum(bool(v) and mode=='team_labels' for v,mode in parsed)
        rate=hits/max(1,len(names))
        if hits>=3 and rate>=.6:
            ranking.append((rate,-proposals[i][2]*proposals[i][3],i))
    if not ranking:
        raise ValueError('自动定位的区域没有稳定读到双方比分，请手动框选比分牌')
    return proposals[max(ranking)[2]]


def read_scoreboard(video, box=None, start=None, step=.5, zoom=4., max_seconds=0., team_names=None):
    """Shared API/video OCR path. Failed reads never replace saved evidence."""
    parent=ROOT/'out'/'tmp'
    parent.mkdir(parents=True,exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='sbocr_',dir=parent) as tmp:
        tmpdir=Path(tmp)
        method='manual_box' if box is not None else 'auto_ocr_verified'
        if box is None:
            probe=tmpdir/'probe';probe.mkdir()
            box=locate_scoreboard_ocr(video,probe,team_names)
        x,y,w,h=box
        crops=tmpdir/'crops';crops.mkdir()
        items=grab_crops(video,{'all':[x,y,x+w,y+h]},step,zoom,crops,max_seconds)
        if not items:raise ValueError('没有可读取的比分牌画面，请检查框选区域')
        texts=run_ocr(crops,tmpdir/'ocr.json')
        res=build_events(items,texts,start,team_names=team_names)
        if res['ocr_hit']/len(items)<.5 or any(v is None for v in res['final'].values()):
            raise ValueError('双方比分有效读数不足，请重新手动框选比分牌；未保存本次结果')
        res.update(video=str(video),box=list(box),method=method,n_crops=len(items))
        return res


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="手动框选记分牌 → OCR 读得分事件")
    ap.add_argument("--video", required=True)
    ap.add_argument("--box-home", default=None, help="主队分区域 x0,y0,x1,y1")
    ap.add_argument("--box-away", default=None, help="客队分区域 x0,y0,x1,y1")
    ap.add_argument("--box-all", default=None,
                    help="整条记分牌区域 x0,y0,x1,y1（推荐）：OCR 后按阅读顺序"
                         "匹配队名与比分，并排除节次/计时数字" )
    ap.add_argument("--start", default=None, help="起始比分 home,away（如 27,35）")
    ap.add_argument("--step", type=float, default=0.5, help="抽样间隔（秒）")
    ap.add_argument("--zoom", type=float, default=4.0, help="放大倍数（OCR 关键）")
    ap.add_argument("--max-seconds", type=float, default=0.0)
    ap.add_argument("--out", default="out/sb_events.json")
    a = ap.parse_args(argv)

    boxes = {}
    if a.box_all:
        boxes["all"] = [float(v) for v in a.box_all.split(",")]
    for team, spec in (("home", a.box_home), ("away", a.box_away)):
        if spec and not a.box_all:
            boxes[team] = [float(v) for v in spec.split(",")]
    if not boxes:
        print("[err] 至少给一个区域：--box-home 或 --box-away")
        return 2
    start = dict(zip(("home", "away"), map(int,a.start.split(",")))) if a.start else {}

    # 临时目录放在**工作区内**：系统 %TEMP% 在受限沙箱里不可写（实测被判 permission denied），
    # 而且工作区内也方便出问题时人工检查中间小图。
    tmpdir = ROOT / "out" / "tmp" / "sbocr"
    if tmpdir.exists():
        shutil.rmtree(tmpdir, ignore_errors=True)
    tmpdir.mkdir(parents=True, exist_ok=True)
    try:
        items = grab_crops(a.video, boxes, a.step, a.zoom, tmpdir,
                           max_seconds=a.max_seconds)
        print(f"抽样 {len(items)} 张小图（{len(boxes)} 个区域）→ OCR …")
        texts = run_ocr(tmpdir, ROOT / "out" / "tmp" / "sb_ocr.json")
        res = build_events(items, texts, start)
        res.update({"video": a.video, "boxes": boxes, "n_crops": len(items)})
        outp = Path(a.out)
        outp.parent.mkdir(parents=True, exist_ok=True)
        outp.write_text(json.dumps(res, ensure_ascii=False, indent=2),
                        encoding="utf-8")
        print(f"OCR 读出数字 {res['ocr_hit']}/{len(items)} 张")
        print(f"得分事件 {len(res['events'])} 条；最终比分 {res['final']}")
        for e in res["events"]:
            print("   t=%7.2fs  %s  +%d → %d"
                  % (e["t"], e["team"], e["delta"], e["value"]))
        if res["rejected"]:
            print(f"被过滤的误读 {len(res['rejected'])} 条（前 5）：")
            for d in res["rejected"][:5]:
                print("   t=%7.2fs %s 读=%s 前值=%s  %s"
                      % (d["t"], d["team"], d["read"], d["prev"], d["why"]))
        print(f"\n已存 {outp}")
        return 0
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
