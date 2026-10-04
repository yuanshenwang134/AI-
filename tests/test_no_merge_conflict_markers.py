"""仓库里**不许存在合并冲突标记**。

为什么单独写这一条（真实事故）：
  有一次我先 `git merge --allow-unrelated-histories`（刻意保留远端历史），
  36 个文件因此产生 add/add 冲突；随后那条本意是"全部取本地版本"的
  `git checkout --ours -f -- .` **因为 -f 与 --ours 不能同用而失败**，
  但我没看退出码就继续 `git add -A` + 提交 + 推送 ——
  结果 **36 个文件带着 `<<<<<<<` `=======` `>>>>>>>` 被推到 GitHub**，
  其中 `src/aihoop/court.py` 直接 `SyntaxError`，整个后端都导不进来。

这类事故的可怕之处：它**不会在提交时报警**，只有真去 import / 跑服务时才炸，
而且那时候可能已经推上去了。所以这里用一条测试把它钉死。

注意：本文件自身的字符串里会出现这些标记（作为要查找的模式），
所以检查时要跳过测试目录里的这个文件。
"""
from __future__ import annotations

import os
import subprocess
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# 只检查真正会参与构建/运行的文本类型；二进制与第三方压缩包跳过
SKIP_EXT = (".npz", ".pt", ".pth", ".onnx", ".png", ".jpg", ".jpeg", ".gif",
            ".ico", ".zip", ".whl", ".min.js")
SKIP_PATH_PARTS = ("web/vendor/", "_tmp/", "out/", "tools/", "third_party/",
                   "_pylibs/")
# 本文件自己含有这些模式（是要查找的字面量），必须跳过
SELF = os.path.join("tests", "test_no_merge_conflict_markers.py")

BEGIN = "<<" * 3 + "<<< "
HEAD = "=" * 7
END = ">>>>>>> "


def _tracked_files() -> list:
    try:
        out = subprocess.run(["git", "ls-files"], cwd=ROOT, capture_output=True,
                             text=True, errors="replace", timeout=60).stdout
    except Exception:                                    # noqa: BLE001
        return []
    return [ln.strip() for ln in out.splitlines() if ln.strip()]


def test_no_conflict_markers_in_tracked_files():
    files = _tracked_files()
    if not files:
        pytest.skip("拿不到 git 文件列表（不在仓库里 / git 不可用）")

    bad = []
    for rel in files:
        rel_posix = rel.replace("\\", "/")
        if rel_posix == SELF.replace("\\", "/"):
            continue
        if rel.endswith(SKIP_EXT) or any(p in rel_posix for p in SKIP_PATH_PARTS):
            continue
        p = os.path.join(ROOT, rel)
        if not os.path.isfile(p):
            continue
        try:
            with open(p, "r", encoding="utf-8", errors="replace") as f:
                text = f.read()
        except OSError:
            continue
        if BEGIN in text or END in text:
            bad.append(rel)

    assert not bad, (
        "以下文件含未解决的合并冲突标记（会把整个后端弄成 SyntaxError）：\n  "
        + "\n  ".join(bad)
        + "\n修法：从冲突前的提交取回干净版本，例如\n"
          "  git checkout <merge 之前的 commit> -- <文件…>\n"
          "确认没有再提交，并跑一遍 tests/。"
    )


def test_head_commit_has_no_conflict_markers():
    """已提交的内容也要查一遍 —— 事故里正是"提交里带着标记"。

    工作区干净但历史里坏着，一样会让别人 clone 下来跑不起来。
    """
    files = _tracked_files()
    if not files:
        pytest.skip("拿不到 git 文件列表")
    try:
        for rel in files:
            rel_posix = rel.replace("\\", "/")
            if rel_posix == SELF.replace("\\", "/"):
                continue
            if rel.endswith(SKIP_EXT) or any(p in rel_posix for p in SKIP_PATH_PARTS):
                continue
            body = subprocess.run(["git", "show", "HEAD:%s" % rel], cwd=ROOT,
                                  capture_output=True, text=True,
                                  errors="replace", timeout=30).stdout
            assert BEGIN not in body and END not in body, \
                "已提交的内容里含冲突标记：%s" % rel
    except FileNotFoundError:                            # noqa: BLE001
        pytest.skip("git 不可用")
