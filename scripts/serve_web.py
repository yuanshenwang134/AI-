"""给演示用的静态文件服务器 —— 比 `python -m http.server` 多一件事：
**禁止缓存 HTML**。

为什么必须这么做（实测踩到）：
  `python -m http.server` 用 SimpleHTTPRequestHandler，只发 Last-Modified、
  不发 Cache-Control。浏览器于是按启发式缓存 index.html ——
  而 index.html 恰恰是**引用所有前端脚本的那张表**：

      <script src="./pages/upload.js?v=42-mini4pt"></script>

  HTML 被缓存住 = 永远加载旧的 upload.js = **新写的界面永远不出现**。
  用户实测就是这个症状：后端已经是最新代码（`stale: false`），
  页面上却看不到新加的「只点 4 个点」开关，于是以为功能没做出来。

  给 JS/CSS 加 `?v=` 只能解决"脚本本身"的缓存，**解决不了引用它们的 HTML**。

用法：
    python scripts/serve_web.py [端口]        # 默认 8080
"""
from __future__ import annotations

import functools
import http.server
import os
import socketserver
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
# 不需要缓存的文件类型（改完刷新就该看到新的）
NO_STORE_EXT = {".html", ".htm", ".js", ".css", ".json", ".jsonl"}


class Handler(http.server.SimpleHTTPRequestHandler):
    def end_headers(self) -> None:
        p = self.path.split("?", 1)[0].lower()
        if os.path.splitext(p)[1] in NO_STORE_EXT:
            # no-store 比 no-cache 更彻底：连条件请求都不走，直接重新拿
            self.send_header("Cache-Control", "no-store, must-revalidate")
            self.send_header("Pragma", "no-cache")
            self.send_header("Expires", "0")
        super().end_headers()

    def log_message(self, fmt, *args):        # 少刷屏
        pass


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    port = int(argv[0]) if argv else 8080
    handler = functools.partial(Handler, directory=str(ROOT))
    socketserver.TCPServer.allow_reuse_address = True
    with socketserver.ThreadingTCPServer(("127.0.0.1", port), handler) as httpd:
        print(f"本地服务已启动：http://127.0.0.1:{port}/web/index.html", flush=True)
        print("（HTML/JS/CSS 一律 no-store —— 改完代码刷新就能看到，不用清缓存）",
              flush=True)
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
