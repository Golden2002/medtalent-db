# -*- coding: utf-8 -*-
"""
ops/tests/fixture_server.py —— 本地招聘站点 fixture（T07 测试用）

把 ops/fixtures/jd/fixture_careers 当作一个真实站点提供服务：
  /                职位列表索引
  /jobs/<slug>.html 每份 mock 招聘页
  /robots.txt       Allow: /jobs/  Disallow: /private/

这样采集管道面对的是**真实的 HTTP 请求与真实的 robots.txt**，
而不是被打桩的函数——合规门禁与限速逻辑都能被真实执行到。

用法：python ops/tests/fixture_server.py --port 8098
"""
import argparse
import functools
import os
import sys
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

sys.stdout.reconfigure(encoding="utf-8")

ROOT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                    "fixtures", "jd", "fixture_careers")


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *a, **kw):
        super().__init__(*a, directory=ROOT, **kw)

    def log_message(self, fmt, *args):
        pass  # 静默，避免刷屏

    def do_GET(self):  # noqa: N802
        # /jobs/<slug>.html -> <slug>.html
        if self.path.startswith("/jobs/"):
            self.path = "/" + self.path[len("/jobs/"):]
        elif self.path == "/":
            self.path = "/index.html"
        return super().do_GET()


def serve(port=8098):
    return ThreadingHTTPServer(("127.0.0.1", port), Handler)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8098)
    a = ap.parse_args()
    srv = serve(a.port)
    print("[✓] fixture 站点： http://127.0.0.1:%d/  （根目录 %s）" % (a.port, ROOT))
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n[=] 已停止")


if __name__ == "__main__":
    main()
