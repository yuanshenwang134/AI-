"""高光剪辑 —— 套餐 A 的「高光集锦」功能。

策略（很重要，答辩讲得清）：
  1. **不重新编码整段视频**：用 ffmpeg 的 `-ss/-to` + `-c copy` 快速切片；
     但流复制无法帧精确，所以默认对每个片段做一次「重编码」以保证
     起止准确（-c:v libx264 -preset veryfast -crf 23）。
  2. **没有 ffmpeg 也不报错**：产出 clips.json（每个片段的精确时间码），
     前端/剪映可以照着时间码手工剪。这样在任何机器上 demo 都不会挂。
  3. **可选加比分字幕**：用 drawtext 打上「主队 42:38 客队」。
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from .model import Shot, ShotValue
from .rules import RulesConfig, zone_of


@dataclass
class Clip:
    index: int
    t: float
    start: float
    end: float
    made: bool
    value: int
    team: str
    player_id: str
    label: str
    path: Optional[str] = None
    available: bool = False
<<<<<<< HEAD
    result: str = "missed"
=======
>>>>>>> a85d267883743e2b264c8702f6b94fcd5f78480f

    def to_dict(self) -> dict:
        return self.__dict__.copy()


<<<<<<< HEAD
def _bundled_bin(name: str) -> Optional[str]:
    """先找项目自带的 ffmpeg/ffprobe，再找 PATH。

    为什么要"自带优先"：这台机器（以及任何全新机器）上 ffmpeg 往往根本没装，
    用户看到的就是"高光集锦一个片段都出不来"。把便携版 ffmpeg 解压到
    项目根的 `tools/ffmpeg/bin/`（见 tools_fetch_ffmpeg.py）即可开箱可用，
    不必改系统 PATH —— 本工具是本地/内网单机部署，装到系统里反而更难回滚。
    """
    exe = name + (".exe" if os.name == "nt" else "")
    here = Path(__file__).resolve()
    for cand in (here.parents[2] / "tools" / "ffmpeg" / "bin" / exe,
                 here.parents[2] / "aihoop_assets" / "ffmpeg" / "bin" / exe):
        if cand.is_file():
            return str(cand)
    return shutil.which(name)


def ffmpeg_path() -> Optional[str]:
    return _bundled_bin("ffmpeg")


def ffprobe_path() -> Optional[str]:
    return _bundled_bin("ffprobe")
=======
def ffmpeg_path() -> Optional[str]:
    return shutil.which("ffmpeg")


def ffprobe_path() -> Optional[str]:
    return shutil.which("ffprobe")
>>>>>>> a85d267883743e2b264c8702f6b94fcd5f78480f


def rank_shots(shots: list[Shot], limit: int = 15) -> list[Shot]:
    """给每次出手打「精彩度」分，选出高光。

    打分依据（可按自己比赛调整）：
      * 三分命中 > 两分命中 > 罚球命中
      * 距离越远越精彩
      * 第四节/最后时刻加权
      * 有防守干扰（contest 高）加权
      * 未命中的精彩球（被封盖/超远三分）也给一点分
    """
    def score(s: Shot) -> float:
        v = 0.0
        if s.made:
            v += 3.0 if s.value == ShotValue.THREE.value else \
                 (1.0 if s.value == ShotValue.FREE_THROW.value else 1.8)
        else:
            v += 0.4
        v += min(s.distance, 10.0) * 0.15
        v += max(0, s.period - 2) * 0.5
        v += s.contest * 0.6
        return v

    return sorted(shots, key=score, reverse=True)[:limit]


def make_highlights(shots: list[Shot], video_path: Optional[str],
                    out_dir: str, limit: int = 15,
                    cfg: Optional[RulesConfig] = None,
                    duration: Optional[float] = None) -> list[dict]:
    cfg = cfg or RulesConfig()
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    picks = rank_shots(shots, limit)

    clips: list[Clip] = []
    for i, s in enumerate(picks):
<<<<<<< HEAD
        # 旧产物的 0/0 表示未提供范围；合法范围必须覆盖出手时刻。
        explicit = (s.clip_start is not None and s.clip_end is not None
                    and 0 <= s.clip_start <= s.t < s.clip_end)
        start = max(0.0, s.clip_start if explicit else s.t - cfg.clip_pad_before)
        end = s.clip_end if explicit else s.t + cfg.clip_pad_after
=======
        start = max(0.0, s.t - cfg.clip_pad_before)
        end = s.t + cfg.clip_pad_after
>>>>>>> a85d267883743e2b264c8702f6b94fcd5f78480f
        if duration:
            end = min(end, duration)
        label = _label(s)
        clips.append(Clip(index=i, t=round(s.t, 2), start=round(start, 2),
                          end=round(end, 2), made=s.made, value=s.value,
<<<<<<< HEAD
                          team=s.team, player_id=s.player_id, label=label, result=s.result))
=======
                          team=s.team, player_id=s.player_id, label=label))
>>>>>>> a85d267883743e2b264c8702f6b94fcd5f78480f

    ff = ffmpeg_path()
    note = ""
    if not video_path or not os.path.exists(video_path):
        note = ("未提供可用视频文件，仅导出片段时间码（clips.json）。"
                "拿到视频后用 scripts/make_highlights.ps1 或本模块重新生成即可。")
    elif not ff:
        note = ("未检测到 ffmpeg，无法自动切片，仅导出时间码。"
<<<<<<< HEAD
                "安装：跑一次 `python tools_fetch_ffmpeg.py`（下到项目 tools/ffmpeg/bin/），"
                "或 winget install Gyan.FFmpeg 后加入 PATH。")
=======
                "安装：winget install Gyan.FFmpeg ，或到 ffmpeg.org 下载后加入 PATH。")
>>>>>>> a85d267883743e2b264c8702f6b94fcd5f78480f
    else:
        for c in clips:
            dst = out / f"clip_{c.index:02d}_t{c.t:07.2f}.mp4"
            ok = _cut(video_path, dst, c.start, c.end)
            c.path = str(dst) if ok else None
            c.available = ok
        # 拼成合集
        playlist = out / "concat.txt"
        good = [c for c in clips if c.available]
        if good:
            with open(playlist, "w", encoding="utf-8") as f:
                for c in good:
                    f.write(f"file '{Path(c.path).as_posix()}'\n")
            reel = out / "highlights_reel.mp4"
            subprocess.run([ff, "-y", "-f", "concat", "-safe", "0",
                            "-i", str(playlist), "-c", "copy", str(reel)],
                           capture_output=True, check=False)
            note = f"已生成 {len(good)} 个片段，合集：{reel.name}"

    index = {"clips": [c.to_dict() for c in clips], "note": note,
             "ffmpeg": bool(ff), "video": video_path,
             "count": len(clips), "available": sum(1 for c in clips if c.available)}
    (out / "index.json").write_text(
        json.dumps(index, ensure_ascii=False, indent=2), encoding="utf-8")

    # 同步给前端用的一份（相对路径）
    for c in index["clips"]:
        if c["path"]:
            c["path"] = Path(c["path"]).name
    (out / "clips.json").write_text(
        json.dumps(index["clips"], ensure_ascii=False, indent=2), encoding="utf-8")
    return index["clips"]


def _cut(src: str, dst: Path, start: float, end: float) -> bool:
    ff = ffmpeg_path()
    if not ff:
        return False
    cmd = [ff, "-y", "-ss", f"{start:.3f}", "-to", f"{end:.3f}", "-i", src,
           "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
           "-c:a", "aac", "-movflags", "+faststart", str(dst)]
    r = subprocess.run(cmd, capture_output=True)
    if r.returncode != 0 or not dst.exists():
        # 回退：流复制（快但不帧精确）
        cmd2 = [ff, "-y", "-ss", f"{start:.3f}", "-to", f"{end:.3f}", "-i", src,
                "-c", "copy", str(dst)]
        r2 = subprocess.run(cmd2, capture_output=True)
        return r2.returncode == 0 and dst.exists()
    return True


def _label(s: Shot) -> str:
<<<<<<< HEAD
    zone = "位置未知" if "location_unknown" in s.tags else zone_of(s.x, s.y)
    kind = {1: "罚球", 2: "两分", 3: "三分"}[s.value]
    if "value_assumed" in s.tags:
        kind = "分值待确认"
    # 分区名里常已含「三分」，避免出现「底角三分三分命中」
    if kind in zone:
        kind = ""
    res = "待确认" if s.result == "unknown" else ("命中" if s.made else "未中")
=======
    zone = zone_of(s.x, s.y)
    kind = {1: "罚球", 2: "两分", 3: "三分"}[s.value]
    # 分区名里常已含「三分」，避免出现「底角三分三分命中」
    if kind in zone:
        kind = ""
    res = "命中" if s.made else "未中"
>>>>>>> a85d267883743e2b264c8702f6b94fcd5f78480f
    return f"第{s.period}节 {_mmss(s.t)} {s.team} {s.player_id} " \
           f"{zone}{kind}{res}"


def _mmss(t: float) -> str:
    t = max(0.0, t)
    return f"{int(t // 60):02d}:{int(t % 60):02d}"


def probe_duration(video_path: str) -> Optional[float]:
    fp = ffprobe_path()
    if not fp:
        return None
    r = subprocess.run([fp, "-v", "error", "-show_entries",
                        "format=duration", "-of",
                        "default=noprint_wrappers=1:nokey=1", video_path],
                       capture_output=True, text=True)
    try:
        return float(r.stdout.strip())
    except Exception:
        return None


def probe_info(video_path: str) -> dict:
    """读视频分辨率/帧率/时长 —— 上传页展示与推算帧号用。"""
    fp = ffprobe_path()
    if not fp:
        return {}
    r = subprocess.run([fp, "-v", "error", "-select_streams", "v:0",
                        "-show_entries", "stream=width,height,r_frame_rate,nb_frames",
                        "-show_entries", "format=duration",
                        "-of", "json", video_path],
                       capture_output=True, text=True)
    try:
        d = json.loads(r.stdout)
        st = (d.get("streams") or [{}])[0]
        fps = 0.0
        if st.get("r_frame_rate", "0/1") not in ("0/0", ""):
            num, _, den = st["r_frame_rate"].partition("/")
            fps = float(num) / float(den or 1)
        return {"width": st.get("width"), "height": st.get("height"),
                "fps": round(fps, 3),
                "duration": float(d.get("format", {}).get("duration", 0) or 0)}
    except Exception:
        return {}
