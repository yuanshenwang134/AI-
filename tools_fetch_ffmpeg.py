"""把便携版 ffmpeg 装进项目：`tools/ffmpeg/bin/ffmpeg.exe`。

为什么装进项目而不是系统 PATH：
  * 本工具是本地/内网单机部署，放项目里好回滚、好打包、也不动用户系统环境；
  * `highlight.ffmpeg_path()` 会**优先**找 `tools/ffmpeg/bin/`，
    所以放好之后"高光集锦"立刻可用，不需要重启系统或改 PATH；
  * 后端进程已经起来也不用重启 —— ffmpeg 是每次切片时现场查路径的。

用法（用后端自己的 python 跑，它所在的环境能上网）：
    python tools_fetch_ffmpeg.py            # 默认下 essentials 版（约 80MB）
    python tools_fetch_ffmpeg.py --keep-zip # 保留下载的 zip
"""
from __future__ import annotations

import argparse
import shutil
import sys
import tempfile
import urllib.request
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DEST = ROOT / "tools" / "ffmpeg" / "bin"
# gyan.dev 的 Windows 构建（essentials：含 ffmpeg / ffprobe / ffplay）
URLS = [
    "https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip",
    "https://github.com/GyanD/codexffmpeg/releases/download/2024-12-26-git-8c1a8c7b1b/ffmpeg-2024-12-26-git-8c1a8c7b1b-essentials_build.zip",
]


def fetch(url: str, dst: Path) -> None:
    print("下载：%s" % url)
    got = 0
    req = urllib.request.Request(url, headers={"User-Agent": "aihoop-setup/1.0"})
    with urllib.request.urlopen(req, timeout=120) as r, open(dst, "wb") as out:
        while True:
            chunk = r.read(1024 * 512)
            if not chunk:
                break
            out.write(chunk)
            got += len(chunk)
            if got % (10 * 1048576) < 1024 * 512:
                print("  已下载 %.0f MB…" % (got / 1048576), flush=True)
    print("  下载完成：%.0f MB" % (got / 1048576))


def extract(zip_path: Path, keep_zip: bool) -> int:
    want = {"ffmpeg.exe", "ffprobe.exe"}
    DEST.mkdir(parents=True, exist_ok=True)
    found: dict[str, Path] = {}
    with zipfile.ZipFile(zip_path) as z:
        for info in z.infolist():
            name = Path(info.filename).name.lower()
            if name in want:
                # 解开到临时文件再搬过去，避免半截文件被当成装好了
                with z.open(info) as src, tempfile.NamedTemporaryFile(
                        delete=False, dir=str(DEST), suffix=".part") as tmp:
                    shutil.copyfileobj(src, tmp)
                tmp_path = Path(tmp.name)
                target = DEST / Path(info.filename).name
                tmp_path.replace(target)
                found[name] = target
                print("  解出：%s（%.1f MB）"
                      % (target.name, target.stat().st_size / 1048576))
    if not keep_zip:
        try:
            zip_path.unlink()
        except OSError:
            pass
    if "ffmpeg.exe" not in found:
        print("  [失败] zip 里没找到 ffmpeg.exe")
        return 1
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--keep-zip", action="store_true")
    ap.add_argument("--zip", default=None, help="用本地已有的 zip（跳过下载）")
    args = ap.parse_args()

    if args.zip:
        return extract(Path(args.zip), True)

    tmp_zip = Path(tempfile.gettempdir()) / "ffmpeg-release-essentials.zip"
    last = None
    for url in URLS:
        try:
            fetch(url, tmp_zip)
            rc = extract(tmp_zip, args.keep_zip)
            if rc == 0:
                print("\n[ok] ffmpeg 已就位：%s" % (DEST / "ffmpeg.exe"))
                print("     后端不需要重启：切片时每次都会现场查这个路径。")
                return 0
        except Exception as e:                              # noqa: BLE001
            last = e
            print("  失败：%s: %s" % (type(e).__name__, e))
    print("\n[失败] 没装上：%s" % last)
    print("     可以手动下载 https://www.gyan.dev/ffmpeg/builds/ 里的 "
          "release-essentials.zip，解压后把 bin/ffmpeg.exe、bin/ffprobe.exe "
          "放到 %s" % DEST)
    return 1


if __name__ == "__main__":
    sys.exit(main())
