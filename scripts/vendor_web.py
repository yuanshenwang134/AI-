"""把前端依赖下载到 web/vendor/，实现「离线也能演示」。

为什么需要：web/index.html 默认从 unpkg / jsdelivr 加载 Vue、Element Plus、ECharts。
答辩现场的网络经常不可靠（甚至完全断网），一个加载失败就是白屏 ——
对比赛来说这是不可接受的风险。这个脚本把 5 个文件抓到本地，
之后 index.html 会**优先用本地副本**（没有才回退到 CDN）。

用法（在 aihoopanalyst 目录下）：

    python scripts/vendor_web.py            # 下载
    python scripts/vendor_web.py --check    # 只检查本地是否齐全

下载完成后建议 git 提交 web/vendor/ 与 web/index.html，
这样队友克隆下来断网也能直接演示。
"""
from __future__ import annotations

import argparse
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
VENDOR = ROOT / "web" / "vendor"

# (本地文件名, 远程地址)  —— 文件名必须与 web/index.html 里引用的一致
ASSETS = [
    ("vue.global.prod.js",
     "https://unpkg.com/vue@3.4.38/dist/vue.global.prod.js"),
    ("element-plus.css",
     "https://unpkg.com/element-plus@2.8.4/dist/index.css"),
    ("element-plus.full.min.js",
     "https://unpkg.com/element-plus@2.8.4/dist/index.full.min.js"),
    ("element-plus-locale-zh-cn.min.js",
     "https://unpkg.com/element-plus@2.8.4/dist/locale/zh-cn.min.js"),
    ("echarts.min.js",
     "https://cdn.jsdelivr.net/npm/echarts@5.5.1/dist/echarts.min.js"),
]

# 兜底镜像：unpkg 挂了就试 jsdelivr（同一版本）
MIRRORS = {
    "vue.global.prod.js": "https://cdn.jsdelivr.net/npm/vue@3.4.38/dist/vue.global.prod.js",
    "element-plus.css": "https://cdn.jsdelivr.net/npm/element-plus@2.8.4/dist/index.css",
    "element-plus.full.min.js": "https://cdn.jsdelivr.net/npm/element-plus@2.8.4/dist/index.full.min.js",
    "element-plus-locale-zh-cn.min.js": "https://cdn.jsdelivr.net/npm/element-plus@2.8.4/dist/locale/zh-cn.min.js",
}


def fetch(url: str, timeout: int = 60) -> bytes:
    req = urllib.request.Request(url, headers={
        "User-Agent": "Mozilla/5.0 (vendor_web.py)",
        "Accept": "*/*",
    })
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def check() -> int:
    missing = [name for name, _ in ASSETS if not (VENDOR / name).exists()]
    for name, _ in ASSETS:
        p = VENDOR / name
        if p.exists():
            print(f"  [OK]   {name:<38} {p.stat().st_size:>10,} bytes")
        else:
            print(f"  [MISS] {name}")
    if missing:
        print(f"\n缺少 {len(missing)} 个文件。运行：python scripts/vendor_web.py")
        return 1
    print("\n本地依赖齐全，前端可离线演示。")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="把前端 CDN 依赖下载到 web/vendor/")
    ap.add_argument("--check", action="store_true", help="只检查，不下载")
    args = ap.parse_args(argv)

    if args.check:
        return check()

    VENDOR.mkdir(parents=True, exist_ok=True)
    ok = fail = 0
    for name, url in ASSETS:
        if (VENDOR / name).exists() and (VENDOR / name).stat().st_size > 1024:
            print(f"  [SKIP] {name} 已存在")
            ok += 1
            continue
        got = None
        for attempt, u in enumerate([url, MIRRORS.get(name)]):
            if not u:
                continue
            tag = "主源" if attempt == 0 else "镜像"
            try:
                print(f"  下载 {name}（{tag}）…")
                got = fetch(u)
                if len(got) < 1024:
                    raise ValueError(f"内容过小（{len(got)} 字节），可能不是有效文件")
                break
            except Exception as e:  # noqa: BLE001
                print(f"    失败：{type(e).__name__}: {e}")
        if got is None:
            print(f"  [FAIL] {name} 两个源都失败")
            fail += 1
            continue
        (VENDOR / name).write_bytes(got)
        print(f"  [OK]   {name}  {len(got):,} bytes")
        ok += 1

    print(f"\n完成：{ok} 成功 / {fail} 失败")
    if fail:
        print("提示：网络不通时，可以让有网的队友跑本脚本后把 web/vendor/ 发给你。")
        return 1
    print("现在直接打开 web/index.html 即可离线演示（会自动优先用本地副本）。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
