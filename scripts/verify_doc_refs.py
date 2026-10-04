"""校验实施手册里引用的「文件 / 函数」是否真实存在。

动机：手册是靠引用骨架里的文件名和函数名讲故事的。一旦代码改名而手册没改，
读者会照着找不到东西，而且不会报错。这个脚本把手册里所有
`src\\aihoop\\xxx.py` 形式的路径，以及 `xxx()` 形式的函数名抽出来对账。

用法：
    python scripts/verify_doc_refs.py
    python scripts/verify_doc_refs.py --verbose
"""
from __future__ import annotations

import argparse
import ast
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src" / "aihoop"
DOCS = sorted((ROOT / "docs").glob("*.md"))

# 允许引用的"待创建"模块（手册明确要求读者自己写）
PLANNED = {"attempts.py", "calibrate.py", "ocr.py", "netmotion.py", "rules_patch.py"}

# 允许引用的"待实现"函数（属于上面那些待创建模块，或手册明确标注要读者补的）
PLANNED_NAMES = {
    "check_frame_consistency",   # 手册里让读者自己写的坐标系自检脚本
    "estimate_z",                # 手册 §5 让读者补的单目高度估计
    "extract_attempts",          # VideoSource 里唯一真正缺的方法
    "overlay_check",             # calibrate.py（需自行创建）
    "resolve_hoop",              # calibrate.py（需自行创建）
    "pick_corners_interactive",  # calibrate.py（需自行创建）
    "detect_net_motion",         # netmotion.py（需自行创建，选做）
    "refine_with_net",           # netmotion.py（选做）
    "ocr_scoreboard",            # ocr.py（需自行创建）
    "interpolate_track",         # attempts.py（需自行创建）
    "detect_release_points",     # attempts.py（需自行创建）
    "annotate",                  # attempts.py（需自行创建）
}


def real_defs() -> tuple[set[str], set[str]]:
    """返回 (骨架里定义的所有名字，含类方法, 所有文件名)"""
    names: set[str] = set()
    files: set[str] = set()
    for p in sorted(SRC.glob("*.py")):
        files.add(p.name)
        try:
            tree = ast.parse(p.read_text(encoding="utf-8"))
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                names.add(node.name)          # 含类方法
            elif isinstance(node, ast.Assign):
                for t in node.targets:
                    if isinstance(t, ast.Name):
                        names.add(t.id)
            elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
                names.add(node.target.id)
    return names, files


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args(argv)

    names, files = real_defs()
    print(f"骨架定义：{len(names)} 个顶层名字，{len(files)} 个模块\n")

    bad_file = bad_name = 0
    for doc in DOCS:
        text = doc.read_text(encoding="utf-8", errors="replace")

        # 1) 引用的 src\aihoop\xxx.py（容忍反斜杠/正斜杠）
        ref_files = set(re.findall(r"src[\\/]aihoop[\\/]([A-Za-z_]\w*\.py)", text))
        unknown = sorted(f for f in ref_files if f not in files and f not in PLANNED)
        if unknown:
            bad_file += len(unknown)
            print(f"✗ {doc.name}: 引用了不存在的模块 {unknown}")

        # 2) 引用的 `name()` 形式函数（只检查看起来属于本工程的）
        #    排除常见第三方/内置调用
        EXCLUDE = {
            "print", "open", "len", "range", "str", "int", "float", "list", "dict",
            "set", "tuple", "input", "enumerate", "zip", "sorted", "sum", "min",
            "max", "abs", "round", "isinstance", "getattr", "setattr", "hasattr",
            "type", "format", "join", "append", "keys", "items", "values", "get",
            "update", "read_text", "write_text", "exists", "mkdir", "glob", "load",
            "save", "dumps", "loads", "parse", "main", "assertEqual", "raises",
            "tmp_path", "fixture", "parametrize", "run", "app", "TestClient",
        }
        ref_names = set(re.findall(r"`([a-z_][a-z0-9_]*)\(\)`", text))
        unknown_n = sorted(n for n in ref_names
                           if n not in names and n not in EXCLUDE
                           and n not in PLANNED_NAMES
                           and not n.startswith("_"))
        if unknown_n:
            bad_name += len(unknown_n)
            print(f"✗ {doc.name}: 引用了不存在的函数 {unknown_n}")
            if args.verbose:
                print("   （如果是第三方 API，请加进 EXCLUDE 白名单）")

    total = bad_file + bad_name
    print(f"\n结论：{'全部引用有效' if total == 0 else f'{total} 处引用对不上'}")
    return 1 if total else 0


if __name__ == "__main__":
    raise SystemExit(main())
