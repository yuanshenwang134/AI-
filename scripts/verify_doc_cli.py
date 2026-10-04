"""校验手册里的 CLI 调用与实际 argparse 子命令/参数是否一致。

动机：文档里的命令行是读者直接复制执行的。子命令拼错（如把 demo 写成 synth）
或参数名写错（--calib vs --cal）会让读者一上手就报错。

做法：**逐行解析**，遇到 `aihoop.cli <sub>` 就记下子命令，
把该行以及后续的续行（PowerShell 反引号 ` / cmd ^ 结尾）上的 `--flag` 都收进来，
再和实际 argparse 的可用参数比对。

用法：
    python scripts/verify_doc_cli.py
    python scripts/verify_doc_cli.py --verbose
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

DOCS = sorted((ROOT / "docs").glob("*.md"))

# 不属于 aihoop.cli 的独立脚本（各有自己的 argparse），出现这些就不判子命令
OTHER_SCRIPTS = ("annotate_attempts", "score_accuracy", "vendor_web", "calibrate.py")

# 全局参数：任何子命令都不该被这些判错（本项目没有，留作扩展）
GLOBAL_FLAGS = {"--help", "-h"}


def real_cli() -> tuple[set[str], dict[str, set[str]]]:
    from aihoop.cli import build_parser
    parser = build_parser()
    subs: set[str] = set()
    opts: dict[str, set[str]] = {}
    for action in parser._actions:  # noqa: SLF001
        if isinstance(action, argparse._SubParsersAction):  # noqa: SLF001
            for name, sub in action.choices.items():
                subs.add(name)
                got: set[str] = set()
                for a in sub._actions:  # noqa: SLF001
                    got.update(a.option_strings)
                opts[name] = got
    return subs, opts


CLI_RE = re.compile(r"aihoop\.cli\s+([a-z][a-z0-9_-]*)")
FLAG_RE = re.compile(r"(--[a-z][a-z0-9-]*)")
CONT_RE = re.compile(r"[`^]\s*$")          # 续行符


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args(argv)

    subs, opts = real_cli()
    print(f"aihoop.cli 实际子命令: {sorted(subs)}")
    for s in sorted(subs):
        print(f"  {s:<10} {sorted(opts[s])}")
    print()

    problems = 0
    for doc in DOCS:
        lines = doc.read_text(encoding="utf-8", errors="replace").splitlines()
        i = 0
        while i < len(lines):
            m = CLI_RE.search(lines[i])
            if not m:
                i += 1
                continue
            sub = m.group(1)
            # 收集本行 + 续行
            chunk = lines[i]
            j = i
            while CONT_RE.search(lines[j]) and j + 1 < len(lines):
                j += 1
                chunk += "\n" + lines[j]
            ctx = "\n".join(lines[max(0, i - 4):i + 1])

            if not any(s in ctx for s in OTHER_SCRIPTS):
                if sub not in subs:
                    print(f"✗ {doc.name}:{i+1}  子命令 `{sub}` 不存在"
                          f"（可用：{sorted(subs)}）")
                    problems += 1
                else:
                    for flag in FLAG_RE.findall(chunk):
                        if flag in GLOBAL_FLAGS:
                            continue
                        if flag not in opts[sub]:
                            print(f"✗ {doc.name}:{i+1}  `{sub}` 不接受 {flag}"
                                  f"（可用：{sorted(opts[sub])}）")
                            problems += 1
            i = j + 1

    print(f"\n结论：{'CLI 调用全部有效' if problems == 0 else f'{problems} 处不一致'}")
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
