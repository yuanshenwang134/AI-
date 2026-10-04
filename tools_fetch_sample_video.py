"""从 archive.org 挑一段能直接用于标定的篮球视频并下载到 data/uploads/。

为什么用它：archive.org 上的校园/社区比赛录像通常是**固定机位 + 整场**，
正好是"一个镜头只拍得到半场、需要左右两个画面各标一侧"的那类素材。

用法（用后端自己的 python 跑，它所在的环境能上网）：
    python tools_fetch_sample_video.py
"""
from __future__ import annotations

import json
import sys
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DEST_DIR = ROOT / "data" / "uploads"

# 候选条目：校园/社区比赛整场录像（identifier 来自 archive.org 搜索）
CANDIDATES = [
    "Boys_Basketball_-_Fairfield_at_Sycamore",
    "marshtvmav-MCN_MHS_s_First_Unified_Basketball_Home_Game",
]

API = "https://archive.org/metadata/%s"
# 太大就别下了：整场录像动辄 1~3GB，先用小的那一档
MAX_MB = 400.0


def pick_file(meta: dict) -> dict | None:
    files = meta.get("files") or []
    vids = []
    for f in files:
        name = f.get("name") or ""
        if not name.lower().endswith((".mp4", ".m4v", ".webm", ".mkv")):
            continue
        try:
            size = float(f.get("size") or 0)
        except (TypeError, ValueError):
            size = 0.0
        vids.append({"name": name, "size": size,
                     "format": (f.get("format") or "")})
    if not vids:
        return None
    # 优先 mp4、体积从小到大里挑一个不超过 MAX_MB 的；都没有就挑最小的
    vids.sort(key=lambda v: (not v["name"].lower().endswith(".mp4"), v["size"]))
    small = [v for v in vids if 0 < v["size"] <= MAX_MB * 1048576]
    return (small or vids)[0]


def download(ident: str, dest: Path) -> bool:
    with urllib.request.urlopen(API % ident, timeout=60) as r:
        meta = json.loads(r.read().decode("utf-8", "replace"))
    f = pick_file(meta)
    if not f:
        print("  [跳过] 这个条目里没有视频文件")
        return False
    url = "https://archive.org/download/%s/%s" % (
        ident, urllib.parse.quote(f["name"]))
    mb = f["size"] / 1048576 if f["size"] else 0
    print("  选中：%s（%.0f MB，%s）" % (f["name"], mb, f["format"]))
    print("  直链：%s" % url)
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".part")
    expect = f["size"] or 0
    got = 0
    with urllib.request.urlopen(url, timeout=120) as resp, open(tmp, "wb") as out:
        while True:
            chunk = resp.read(1024 * 512)
            if not chunk:
                break
            out.write(chunk)
            got += len(chunk)
            if got % (10 * 1048576) < 1024 * 512:
                print("    已下载 %.0f MB…" % (got / 1048576), flush=True)
    # ⚠️ 必须核对大小：archive.org 的连接会中途断掉，urllib 读到 EOF 就当成"下完了"，
    # 结果落盘一个**被截断的 mp4** —— 表现是 ffmpeg 报 "partial file"、后半段根本解不出来
    # （这个坑踩过一次：只下到 147MB / 应为 503MB）。
    if expect and got < expect * 0.99:
        try:
            tmp.unlink()
        except OSError:
            pass
        print("    [失败] 只下到 %.0f MB / 应为 %.0f MB —— 连接被截断，已删除"
              % (got / 1048576, expect / 1048576))
        return False
    tmp.replace(dest)
    print("  完成：%s（%.0f MB）" % (dest, got / 1048576))
    return True


def cut(src: Path, dst: Path, start: float, dur: float,
        ffmpeg: str | None = None) -> bool:
    """从整场里剪一段当示例（`-c copy` 秒级完成，不重编码）。

    为什么要剪：整场一个多小时，CPU 上跑一次分析要十几小时 —— 当示例素材
    不合适。3 分钟足够展示比分/出手/热区/战术图。
    """
    import subprocess
    if ffmpeg is None:
        sys.path.insert(0, str(ROOT / "src"))
        from aihoop.highlight import ffmpeg_path
        ffmpeg = ffmpeg_path()
    if not ffmpeg:
        print("  找不到 ffmpeg：先跑 python tools_fetch_ffmpeg.py")
        return False
    dst.parent.mkdir(parents=True, exist_ok=True)
    cmd = [ffmpeg, "-hide_banner", "-loglevel", "error", "-ss", "%.2f" % start,
           "-t", "%.2f" % dur, "-i", str(src), "-c", "copy", "-y", str(dst)]
    subprocess.run(cmd, check=False)
    if not dst.exists() or dst.stat().st_size < 1024:
        print("  剪切失败：%s" % dst)
        return False
    print("  已剪出示例：%s（%.1f MB，从 %.0fs 起 %.0f 秒）"
          % (dst, dst.stat().st_size / 1048576, start, dur))
    return True


def main() -> int:
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--cut-from", type=float, default=None,
                    help="从已下好的整场里剪一段：起点秒数")
    ap.add_argument("--cut-dur", type=float, default=180.0, help="剪多长（秒）")
    ap.add_argument("--cut-out", default="basketball_match_3min.mp4")
    ap.add_argument("--full", default="basketball_match.mp4")
    ap.add_argument("--cut-start", type=float, default=360.0,
                    help="下载完成后自动剪这一段的起点（默认 360s，即 6:00）")
    ap.add_argument("--no-cut", action="store_true",
                    help="只要整场，不自动剪示例片段")
    args = ap.parse_args()

    DEST_DIR.mkdir(parents=True, exist_ok=True)

    if args.cut_from is not None:
        # 整场可能放在 data/（推荐，不进「示例视频」下拉）或 data/uploads/
        cands = [DEST_DIR / args.full, ROOT / "data" / args.full]
        src = next((p for p in cands if p.exists()), None)
        if src is None:
            print("没有整场素材：%s（先不带 --cut-from 跑一次下载）"
                  % " 或 ".join(str(p) for p in cands))
            return 1
        return 0 if cut(src, DEST_DIR / args.cut_out,
                        args.cut_from, args.cut_dur) else 1

    for ident in CANDIDATES:
        print("[%s]" % ident)
        try:
            # 整场放到 data/ 下（不进 data/uploads，免得被「示例视频」下拉选中后
            # 一跑就是十几个小时），随后只把剪出来的 3 分钟放进 data/uploads。
            full_path = ROOT / "data" / args.full
            if not download(ident, full_path):
                continue
            if args.no_cut:
                return 0
            ok = cut(full_path, DEST_DIR / args.cut_out,
                     args.cut_start, args.cut_dur)
            return 0 if ok else 1
        except Exception as e:                      # noqa: BLE001
            print("  失败：%s: %s" % (type(e).__name__, e))
    return 1


if __name__ == "__main__":
    sys.exit(main())
