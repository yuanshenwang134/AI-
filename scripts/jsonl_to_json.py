"""把 JSONL 转成一个 JSON 对象 —— 前端「无后端演示」用。

后端产出的逐帧战术数据是 JSONL（`tactics_frames.jsonl`，一行一帧）：
流式写入、单行可读、崩溃也不会毁掉整个文件，适合大文件。
但前端在演示模式下读的是静态 JSON（fetch + JSON.parse 最省事），
所以在 `scripts/sync_demo.ps1` 里做一次转换，产出一份 `tactics_frames.json`。

用法：
    python scripts/jsonl_to_json.py out/demo/tactics_frames.jsonl \\
        web/demo/tactics_frames.json
    python scripts/jsonl_to_json.py in.jsonl out.json --key clips
"""
from __future__ import annotations

import argparse
import io
import json


def convert(src: str, dst: str, key: str = "frames",
            available: bool = True) -> int:
    rows = []
    with io.open(src, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    obj = {"available": available, "count": len(rows), key: rows}
    with io.open(dst, "w", encoding="utf-8") as f:
        f.write(json.dumps(obj, ensure_ascii=False))
    return len(rows)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="JSONL -> JSON（演示数据用）")
    ap.add_argument("src")
    ap.add_argument("dst")
    ap.add_argument("--key", default="frames")
    a = ap.parse_args(argv)
    n = convert(a.src, a.dst, a.key)
    print(f"{a.src} -> {a.dst}：{n} 条")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())