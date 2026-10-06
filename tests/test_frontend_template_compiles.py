"""前端自检必须有"**真编译模板**"这一步 —— 否则一个引号错误就让整页空白。

实测事故（这条测试的由来）
--------------------------
我把一个属性写成 `:viewBox="'0 0 1 1"`（字符串没闭合），结果
**页面内容区整块空白**（侧边栏还在，主区什么都不渲染），
而当时的 `web/_validate.js` **是全绿的** ——
它的"模板结构校验"用的是正则，遇到未闭合的 `'` 会一路吞到后面，
看起来仍然是配对的。**正则查不出这类错误，Vue 编译器能。**

所以现在 `_validate.js` 会用仓库自带的 `web/vendor/vue.global.prod.js`
真的编译一遍每个页面模板。这条测试保证：
  1. 自检脚本整体通过（含真编译）；
  2. 真编译这一步**确实加载了**（不是被静默跳过）；
  3. 一个故意写坏的模板会被抓住（防止检查退化成摆设）。
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
VALIDATE = os.path.join(ROOT, "web", "_validate.js")
UPLOAD = os.path.join(ROOT, "web", "pages", "upload.js")


def _run_validate():
    return subprocess.run(["node", "web/_validate.js"], cwd=ROOT,
                          capture_output=True, text=True, errors="replace",
                          timeout=300)


def test_validate_passes():
    if not os.path.exists(VALIDATE):
        pytest.skip("没有 web/_validate.js")
    r = _run_validate()
    out = (r.stdout or "") + (r.stderr or "")
    assert r.returncode == 0, "前端自检没通过：\n" + out[-2500:]


def test_vue_compiler_is_actually_loaded():
    """真编译不能被静默跳过（Vue 没载入时会打印"[提示] …跳过"）。"""
    if not os.path.exists(VALIDATE):
        pytest.skip("没有 web/_validate.js")
    r = _run_validate()
    out = (r.stdout or "") + (r.stderr or "")
    assert "Vue 编译器没载入" not in out, (
        "Vue 编译器没载入，模板真编译检查被跳过了 —— 等于没有这道防线：\n"
        + out[-800:])


def test_broken_template_is_caught():
    """故意写坏一个模板属性，自检必须失败并点名 [tpl-compile]。"""
    if not (os.path.exists(VALIDATE) and os.path.exists(UPLOAD)):
        pytest.skip("缺文件")
    src = open(UPLOAD, encoding="utf-8").read()
    good = ':viewBox="\\\'0 0 1 1\\\'"'
    bad = ':viewBox="\\\'0 0 1 1"'
    if good not in src:
        pytest.skip("找不到 viewBox 那段（写法变了）")
    bak = os.path.join(ROOT, "_tmp", "upload_tplcompile.bak")
    os.makedirs(os.path.dirname(bak), exist_ok=True)
    shutil.copy2(UPLOAD, bak)
    try:
        open(UPLOAD, "w", encoding="utf-8").write(src.replace(good, bad, 1))
        r = _run_validate()
        out = (r.stdout or "") + (r.stderr or "")
        assert r.returncode != 0, "写坏的模板竟然通过了自检"
        assert "[tpl-compile]" in out, (
            "自检失败了，但不是模板编译抓到的：\n" + out[-1200:])
    finally:
        shutil.copy2(bak, UPLOAD)
    r2 = _run_validate()
    assert r2.returncode == 0, "还原后自检又不通过了"
