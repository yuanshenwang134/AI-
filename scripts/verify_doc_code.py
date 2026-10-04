"""校验实施手册里所有 Python 代码块至少语法合法。

为什么需要：文档里的代码片段是读者"照着抄"的东西，一旦有语法错或
用到已删除的常量，读者卡住却不报错，非常难查。这个脚本把手册里的
```python 代码块全部抽出来编译一遍。

用法：
    python scripts/verify_doc_code.py
    python scripts/verify_doc_code.py --show   # 同时打印失败的片段
"""
from __future__ import annotations

import argparse
import ast
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DOCS = sorted((ROOT / "docs").glob("*.md"))

# 文档里刻意写的"反例/错误示范"块，允许编译失败
ALLOW_FAIL_MARKERS = ("反例", "错误示范", "不要这样", "BAD", "✗")

# 说明性代码块：不是给读者直接执行的完整语句，而是"接口速览"。
# 例如 `RawTrack(players: dict[str, Player], ...)` 是签名草图，
# 或单独一行 `"needs_review": [...]` 只是在指出某段实现。
SKETCH_PATTERNS = [
    r"^\s*\w+\s*:\s*\w",                 # 签名草图：参数: 类型
    r"^\s*\w+\s*:\s*[\[\{]",             # players: dict[...] / x: [..]
    r"^\s*[\"']\w+[\"']\s*:",            # 裸字典字面量的一行
]


def looks_like_sketch(code: str) -> bool:
    """判断这个块是"接口速览"还是"可执行代码"。"""
    first = code.lstrip().splitlines()[0] if code.strip() else ""
    for pat in SKETCH_PATTERNS:
        if re.match(pat, first):
            return True
    # 含有裸注解（`名字: 类型,`）且没有任何定义/导入语句 -> 视为接口速览
    if re.search(r"^\s*\w+\s*:\s*[\w\[\]\.]+\s*,?\s*$", code, re.M) and \
       not re.search(r"^\s*(def|class|import|from)\b", code, re.M):
        return True
    # 以 `名字(` 开头且含 `参数: 类型` 的也当签名草图（如 RawTrack(...)）
    if re.match(r"^\s*\w+\s*\(", first) and \
       re.search(r"^\s*\w+\s*:\s*[\w\[\]\.]+", code, re.M):
        return True
    return False


def blocks(text: str):
    """产出 (起始行号, 代码, 块前的上下文)"""
    lines = text.splitlines()
    out = []
    i = 0
    while i < len(lines):
        m = re.match(r"^\s*```(\w*)\s*$", lines[i])
        if m:
            lang = m.group(1).lower()
            start = i + 1
            j = start
            while j < len(lines) and not re.match(r"^\s*```\s*$", lines[j]):
                j += 1
            if lang in ("python", "py"):
                ctx = "\n".join(lines[max(0, start - 4):start])
                out.append((start, "\n".join(lines[start:j]), ctx))
            i = j + 1
        else:
            i += 1
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--show", action="store_true", help="打印失败片段")
    args = ap.parse_args(argv)

    total = bad = skipped = 0
    for doc in DOCS:
        text = doc.read_text(encoding="utf-8", errors="replace")
        bl = blocks(text)
        if not bl:
            continue
        fails = []
        for lineno, code, ctx in bl:
            total += 1
            # 片段里常见的"占位/省略"写法，编译不了是正常的，跳过
            if re.search(r"^\s*\.\.\.\s*$|#\s*\.\.\.|（略）|<你的|xxx", code, re.M):
                skipped += 1
                continue
            # 反例块允许失败
            if any(mk in ctx for mk in ALLOW_FAIL_MARKERS):
                skipped += 1
                continue
            # 接口速览/签名草图不是可执行代码，跳过
            if looks_like_sketch(code):
                skipped += 1
                continue
            try:
                ast.parse(code)
            except SyntaxError as e:
                # 片段从函数体/类体中间截断（缩进开头）是常见的合法情况，
                # 用 dedent 再试一次
                import textwrap
                try:
                    ast.parse(textwrap.dedent(code))
                except SyntaxError:
                    bad += 1
                    fails.append((doc.name, lineno, f"line {e.lineno}: {e.msg}", code))

        print(f"{doc.name}: {len(bl)} 个 python 代码块, 失败 {len(fails)}")
        for name, lineno, msg, code in fails:
            print(f"  ✗ {name}:{lineno}  {msg}")
            if args.show:
                print("    " + "\n    ".join(code.splitlines()[:12]))

    print(f"\n合计 {total} 个代码块，{skipped} 个跳过（占位/省略/反例），{bad} 个语法失败")
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
