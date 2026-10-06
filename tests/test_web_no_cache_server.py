"""演示用的静态服务必须**禁止缓存 HTML**。

背景（实测踩到，用户症状就是它）：
  演示用 `python -m http.server` 时只发 Last-Modified、不发 Cache-Control，
  浏览器于是缓存 index.html —— 而 index.html 正是"引用所有前端脚本的那张表"：

      <script src="./pages/upload.js?v=42-mini4pt"></script>

  HTML 被缓存住 = 永远加载旧的 upload.js = **新写的界面永远不出现**。
  用户看到的现象：后端已经是最新代码（/api/health 的 stale=false），
  页面上却没有新加的「只点 4 个点」开关，于是以为功能没做出来。
  给 JS 加 `?v=` 只能解决脚本本身，解决不了引用它的 HTML。
"""
from __future__ import annotations

import http.client
import os
import socket
import sys
import threading

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


@pytest.fixture(scope="module")
def web_server():
    """真起一个 serve_web.py 的服务，验证响应头（不是读源码猜）。"""
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "serve_web", os.path.join(ROOT, "scripts", "serve_web.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    import functools
    import socketserver

    port = _free_port()
    handler = functools.partial(mod.Handler, directory=str(ROOT))
    socketserver.TCPServer.allow_reuse_address = True
    httpd = socketserver.ThreadingTCPServer(("127.0.0.1", port), handler)
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    yield port
    httpd.shutdown()
    httpd.server_close()


def _headers(port: int, path: str) -> dict:
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    c.request("GET", path)
    r = c.getresponse()
    h = {k.lower(): v for k, v in r.getheaders()}
    c.close()
    return h


def test_html_is_not_cacheable(web_server):
    h = _headers(web_server, "/web/index.html")
    cc = (h.get("cache-control") or "").lower()
    assert "no-store" in cc, \
        "index.html 必须 no-store，否则新前端永远加载不到（实测症状）：%r" % cc


def test_js_and_css_are_not_cacheable(web_server):
    for p in ("/web/pages/upload.js", "/web/styles.css"):
        h = _headers(web_server, p)
        cc = (h.get("cache-control") or "").lower()
        assert "no-store" in cc, "%s 必须 no-store：%r" % (p, cc)


def test_index_html_references_a_versioned_upload_js(web_server):
    """HTML 里引用 upload.js 要带 ?v=，改脚本时才能击穿缓存。"""
    import urllib.request
    with urllib.request.urlopen(
            "http://127.0.0.1:%d/web/index.html" % web_server, timeout=10) as r:
        body = r.read().decode("utf-8", "replace")
    assert "pages/upload.js?v=" in body, "index.html 必须给 upload.js 带版本号"


def test_launcher_uses_the_no_cache_server():
    """启动器必须用 scripts/serve_web.py，而不是 python -m http.server。"""
    p = os.path.join(ROOT, "打开演示.ps1")
    if not os.path.exists(p):
        pytest.skip("没有启动器")
    src = open(p, encoding="utf-8", errors="replace").read()
    assert "serve_web.py" in src, "启动器要用 serve_web.py（带 no-store）"
    assert "-m http.server" not in src, \
        "不能再用 python -m http.server —— 它不发 Cache-Control，会把 HTML 缓存住"


def test_misleading_message_is_gone():
    """那句"没有任何一帧自己够 4 个点"会误导用户（他明明点了 6 个）。"""
    src = open(os.path.join(ROOT, "src", "aihoop", "api.py"),
               encoding="utf-8").read()
    assert "没有任何一帧自己够 4 个点" not in src, \
        "这句措辞不准确：真实含义是【没有一帧的 4 个以上点几何自洽】"
    assert "只在一幅画面上点" in src, \
        "失败提示里要给出最省事的做法：单幅画面就能解，不必凑两幅"
