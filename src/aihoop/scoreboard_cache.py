"""比分牌几何的**按视频**缓存。

为什么单独一个模块：比分牌的框(BoundingBox)是**按机位/按视频**成立的，
不是按分辨率。老做法把它存在一个全局的 `data/scoreboard_bug.json` 里，
于是「上一次跑的那段视频里定位到的框」会被下一段视频**无条件继承**。

这个坑真的踩过，而且故障现象极具误导性：

  一段视频自动定位到的「比分牌」其实是**木地板色块** (406, 634, 398, 75)，
  被写进了全局缓存。换一段室内比赛视频再跑，代码直接复用这个框 →
  整片 1440 帧一帧都读不到 → CLI 打印「比分牌：读到 0/1440 帧」。
  看起来像「这段视频没有比分牌」，其实是缓存串场了，
  自动计分整条路径就这么悄悄失效。

所以现在：
  * 几何按视频指纹（路径 + 分辨率 + 时长）分文件存，互不串场；
  * 复用前先过 `scoreboard.bug_looks_valid()` —— 那段视频里真的能读到白字才用；
  * 旧版遗留的全局 `data/scoreboard_bug.json` **不再读取**（只当历史文件留着）。
"""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Optional

CACHE_NAME = "scoreboard_bugs"
LEGACY_NAME = "scoreboard_bug.json"

_CACHE_DIR = None


def set_cache_dir(path) -> None:
    """覆盖缓存目录（测试/多工程时用）；传 None 恢复默认。"""
    global _CACHE_DIR
    _CACHE_DIR = Path(path) if path else None


def default_cache_dir() -> Path:
    if _CACHE_DIR is not None:
        return Path(_CACHE_DIR)
    root = Path(__file__).resolve().parents[2]
    return root / "data" / CACHE_NAME


def video_key(video_path: str, width: int = 0, height: int = 0,
              duration: float = 0.0) -> str:
    """视频指纹：路径 + 分辨率 + 时长。

    分辨率与时长都算进去是有意的 —— 同一台机器上换一场比赛（路径变了）、
    或者拿裁剪过的片段来跑（时长/分辨率变了），都不该继承上一份几何。
    """
    try:
        p = Path(video_path).resolve()
    except Exception:
        p = Path(str(video_path))
    raw = f"{p.as_posix()}|{int(width)}x{int(height)}|{float(duration):.2f}"
    digest = hashlib.sha1(raw.encode("utf-8")).hexdigest()[:12]
    stem = re.sub(r"[^0-9A-Za-z_.-]+", "_", p.stem)[:32]
    return f"{stem}_{digest}"


def cache_path(video_path: str, width: int = 0, height: int = 0,
               duration: float = 0.0, create_dir: bool = False) -> Path:
    d = default_cache_dir()
    if create_dir:
        d.mkdir(parents=True, exist_ok=True)
    return d / f"{video_key(video_path, width, height, duration)}.json"


def load_bug(video_path: str, width: int = 0, height: int = 0,
             duration: float = 0.0):
    """读回这份视频的比分牌几何；没有就返回 None。"""
    p = cache_path(video_path, width, height, duration)
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None


def save_bug(bug_dict: dict, video_path: str, width: int = 0, height: int = 0,
             duration: float = 0.0) -> Path:
    p = cache_path(video_path, width, height, duration, create_dir=True)
    p.write_text(json.dumps(bug_dict, ensure_ascii=False, indent=2),
                 encoding="utf-8")
    return p


def legacy_path() -> Path:
    """旧版全局缓存的位置（只用于提示用户它已经不再生效）。"""
    root = Path(__file__).resolve().parents[2]
    return root / "data" / LEGACY_NAME
