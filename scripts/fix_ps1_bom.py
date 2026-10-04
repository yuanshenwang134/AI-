<<<<<<< HEAD
"""确保所有 .ps1 脚本都是「UTF-8 带 BOM」，并检查 .bat 的换行与编码。
=======
"""确保所有 .ps1 脚本都是「UTF-8 带 BOM」。
>>>>>>> a85d267883743e2b264c8702f6b94fcd5f78480f

为什么需要（真实踩过的坑）：
    Windows PowerShell 5.1 读取**无 BOM 的 UTF-8** .ps1 时，会按系统 ANSI(GBK)
    解码。中文注释因此乱码，某些字符（如全角引号、破折号）会吞掉换行，
    把下一行代码变成注释的一部分 —— 脚本表面上还在跑，行为却完全不对，
    报错位置也指向莫名其妙的地方，极难排查。

    本工程曾因此让 sync_demo.ps1 里的 `$hlSrc = ...` 被注释掉，
    触发 Test-Path 参数为 null 的诡异错误。

<<<<<<< HEAD
.bat 为什么也要查（2026-09-27 又踩一次）：
    cmd.exe **只认 CRLF**。用 LF 写的 .bat 会被它按行拆错，报出一串
    "'astAPI' is not recognized as an internal or external command" ——
    启动脚本看起来执行了，其实每一行都被截断。另外 .bat 里写中文、文件又没 BOM 时，
    cmd 按控制台代码页（GBK）解 UTF-8 字节 → 乱码。所以这里的规矩是：
    **.bat 一律纯 ASCII + CRLF**（要让用户看到的中文放到 Python 那侧打印）。

=======
>>>>>>> a85d267883743e2b264c8702f6b94fcd5f78480f
用法（在 aihoopanalyst 目录下）：
    python scripts/fix_ps1_bom.py            # 检查并修复
    python scripts/fix_ps1_bom.py --check    # 只检查，不修改（非零退出表示有问题）
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BOM = b"\xef\xbb\xbf"

# 扫描范围：仓库里所有 .ps1（含根目录的启动器）
PATTERNS = ["scripts/*.ps1", "*.ps1"]
<<<<<<< HEAD
# .bat 单独按"纯 ASCII + CRLF"检查
BAT_PATTERNS = ["scripts/*.bat", "*.bat"]
=======
>>>>>>> a85d267883743e2b264c8702f6b94fcd5f78480f


def targets() -> list[Path]:
    out: list[Path] = []
    for pat in PATTERNS:
        out.extend(sorted(ROOT.glob(pat)))
    return out


<<<<<<< HEAD
def bat_targets() -> list[Path]:
    out: list[Path] = []
    for pat in BAT_PATTERNS:
        out.extend(sorted(ROOT.glob(pat)))
    return out


def check_bats(check_only: bool) -> list[Path]:
    """检查 .bat：必须 CRLF；含非 ASCII 就必须先 `chcp 65001`。

    规则为什么是这两条（而不是"一律纯 ASCII"）：
      * **CRLF 是硬要求**。cmd.exe 用 LF 的 .bat 会把一行拆成几段，
        报出一串 "'astAPI' is not recognized..."（2026-09-27 实测踩过）。
      * 中文不是硬伤，但必须配套 `chcp 65001` —— 否则 cmd 按控制台代码页（GBK）
        解 UTF-8 字节，中文变乱码。`打开演示.bat` 就是"UTF-8 + chcp 65001"，
        实测能用，所以不该被判失败。
      * 带 BOM 只作**提醒**：首行有被 cmd 读错的风险，但不致命。
    """
    bad: list[Path] = []
    for p in bat_targets():
        raw = p.read_bytes()
        rel = p.relative_to(ROOT)
        problems, warns = [], []
        if raw.startswith(BOM):
            warns.append("带 BOM（不致命，但首行有被 cmd 读错的风险）")
        lone_lf = raw.count(b"\n") - raw.count(b"\r\n")
        if lone_lf:
            problems.append(f"{lone_lf} 处裸 LF（cmd 只认 CRLF，会把行拆错）")
        body = raw[3:] if raw.startswith(BOM) else raw
        has_nonascii = any(c > 127 for c in body)
        if has_nonascii and b"chcp 65001" not in raw.lower():
            problems.append("含非 ASCII 但没设 chcp 65001（中文会乱码）")
        tag = "[OK]  " if not problems and not warns else (
            "[BAD] " if problems else "[WARN]")
        extra = "；".join(problems + warns)
        print(f"  {tag} {rel}" + (f" —— {extra}" if extra else ""))
        if problems:
            bad.append(p)
            if not check_only:
                text = body.decode("utf-8", "replace").replace("\r\n", "\n") \
                    .replace("\n", "\r\n")
                p.write_bytes(text.encode("utf-8"))
                print(f"  [FIX]  {rel} —— 已把换行统一成 CRLF（BOM/中文保持原样）")
    return bad


=======
>>>>>>> a85d267883743e2b264c8702f6b94fcd5f78480f
def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true", help="只检查，不修改")
    args = ap.parse_args(argv)

    files = targets()
    if not files:
        print("没有找到任何 .ps1 文件")
        return 0

    need = []
    for p in files:
        raw = p.read_bytes()
        if raw.startswith(BOM):
            print(f"  [OK]   {p.relative_to(ROOT)}")
            continue
        # 先确认内容是合法 UTF-8，否则加 BOM 也救不了
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError as e:
            print(f"  [BAD]  {p.relative_to(ROOT)} 不是合法 UTF-8：{e}")
            need.append(p)
            continue
        if args.check:
            print(f"  [MISS] {p.relative_to(ROOT)} 缺少 BOM")
            need.append(p)
            continue
        p.write_bytes(BOM + text.encode("utf-8"))
        print(f"  [FIX]  {p.relative_to(ROOT)} 已补上 BOM")

<<<<<<< HEAD
    print("\n-- .bat（要求纯 ASCII + CRLF）--")
    bad_bats = check_bats(args.check)
    if bad_bats:
        if args.check:
            print(f"\n{len(bad_bats)} 个 .bat 不符合要求 —— cmd.exe 会执行错乱。")
            print("运行 `python scripts/fix_ps1_bom.py` 自动修（会转成 ASCII + CRLF）。")
        return 1

=======
>>>>>>> a85d267883743e2b264c8702f6b94fcd5f78480f
    print()
    if need and args.check:
        print(f"{len(need)} 个 .ps1 缺少 BOM —— 在 Windows PowerShell 5.1 下会乱码。")
        print("运行 `python scripts/fix_ps1_bom.py` 修复。")
        return 1
    if need:
        print(f"{len(need)} 个文件需要人工检查（不是合法 UTF-8）。")
        return 1
<<<<<<< HEAD
    print(f"全部 {len(files)} 个 .ps1 都带 BOM，PowerShell 5.1 可安全读取；"
          f"{len(bat_targets())} 个 .bat 的换行与编码检查通过"
          f"（要求 CRLF；含非 ASCII 时必须设 chcp 65001）。")
=======
    print(f"全部 {len(files)} 个 .ps1 都带 BOM，PowerShell 5.1 可安全读取。")
>>>>>>> a85d267883743e2b264c8702f6b94fcd5f78480f
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
