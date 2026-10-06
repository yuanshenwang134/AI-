"""启动脚本的编码/换行必须合规 —— 直接复用项目自带的检查器，别自己另写一套。

为什么单独立一条测试（这一轮真的把它们弄坏了，用户当场看到报错）：
  1. 我用编辑器改 `打开演示.ps1` 时把 **UTF-8 BOM 弄丢了** →
     Windows PowerShell 5.1 会按系统 ANSI(GBK) 解码该文件 →
     中文注释乱码、全角字符吞掉换行 → 脚本报
     `Missing closing '}' ... ParserError`，**整个演示启动器打不开**。
  2. 我一度把 .bat 写成「UTF-8 无 BOM 且没有 chcp」→ cmd 按 GBK 解 UTF-8 字节
     → 中文被当成命令执行（用户看到满屏 "'爛細' 不是内部或外部命令"）。
  3. 后来又改成 GBK，但文件里仍留着 `chcp 65001`（声明 UTF-8 却存 GBK 字节），
     而且项目自带的 fix_ps1_bom.py 会**按 UTF-8 解码**去修 CRLF，
     遇到 GBK 字节替换成 U+FFFD —— 会把中文改坏（实测已发生）。

  项目里本来就有 `scripts/fix_ps1_bom.py` 管这件事（而且注释里写清了规矩），
  我没有先看它才反复踩。这条测试就是让"以后任何人改这些文件"都必须过它。
"""
from __future__ import annotations

import os
import subprocess
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CHECKER = os.path.join(ROOT, "scripts", "fix_ps1_bom.py")


def test_project_checker_passes():
    """项目自带的检查器必须返回 0（.ps1 带 BOM、.bat 合规）。"""
    if not os.path.exists(CHECKER):
        pytest.skip("没有 scripts/fix_ps1_bom.py")
    r = subprocess.run([sys.executable, CHECKER, "--check"], cwd=ROOT,
                       capture_output=True, text=True, errors="replace",
                       timeout=120)
    out = (r.stdout or "") + (r.stderr or "")
    assert r.returncode == 0, (
        "启动脚本编码/换行不合规 —— 会让用户看到满屏命令错误或脚本直接崩。\n"
        "修法：python scripts/fix_ps1_bom.py\n\n" + out)


def test_all_ps1_have_bom():
    """.ps1 一律 UTF-8 带 BOM —— PowerShell 5.1 没有 BOM 就按 ANSI 解。"""
    bad = []
    for name in os.listdir(ROOT):
        if not name.lower().endswith(".ps1"):
            continue
        p = os.path.join(ROOT, name)
        with open(p, "rb") as f:
            if not f.read(3) == b"\xef\xbb\xbf":
                bad.append(name)
    assert not bad, "这些 .ps1 缺 BOM（PowerShell 会把中文读成乱码）：%s" % bad


def test_bats_are_crlf_and_chcp_when_non_ascii():
    """.bat：必须 CRLF；含非 ASCII 就必须有 chcp 65001。"""
    bad = []
    for name in os.listdir(ROOT):
        if not name.lower().endswith(".bat"):
            continue
        p = os.path.join(ROOT, name)
        raw = open(p, "rb").read()
        lone_lf = raw.count(b"\n") - raw.count(b"\r\n")
        if lone_lf:
            bad.append("%s: %d 处裸 LF" % (name, lone_lf))
        if any(c > 127 for c in raw) and b"chcp 65001" not in raw.lower():
            bad.append("%s: 含非 ASCII 但没有 chcp 65001" % name)
    assert not bad, "这些 .bat 不合规（cmd 会执行错乱或中文乱码）：%s" % bad


def test_launcher_opens_a_cache_busted_url():
    """启动器打开的地址要带时间戳 —— 否则浏览器可能还用缓存的旧页面。"""
    p = os.path.join(ROOT, "打开演示.ps1")
    if not os.path.exists(p):
        pytest.skip("没有启动器")
    src = open(p, encoding="utf-8-sig", errors="replace").read()
    assert "UrlOpen" in src, "启动器应当用带时间戳的 URL 打开浏览器"
