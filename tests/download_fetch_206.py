# -*- coding: utf-8 -*-
"""download.fetch 续传语义单测：206 续传 + 200 全量回退重下。"""
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
import tempfile

import sys
sys.path.insert(0, ".")

BODY = bytes(range(256)) * 4096          # 1MiB 测试体


class RangeOK(BaseHTTPRequestHandler):
    def do_GET(self):
        rng = self.headers.get("Range")
        if rng:
            start = int(rng.split("=")[1].split("-")[0])
            self.send_response(206)
            self.send_header("Content-Range", f"bytes {start}-{len(BODY)-1}/{len(BODY)}")
            self.send_header("Content-Length", str(len(BODY) - start))
            self.end_headers()
            self.wfile.write(BODY[start:])
        else:
            self.send_response(200)
            self.send_header("Content-Length", str(len(BODY)))
            self.end_headers()
            self.wfile.write(BODY)

    def log_message(self, *a):
        pass


class Always200(BaseHTTPRequestHandler):
    def do_GET(self):                     # 忽略 Range，永远回全量 200
        self.send_response(200)
        self.send_header("Content-Length", str(len(BODY)))
        self.end_headers()
        self.wfile.write(BODY)

    def log_message(self, *a):
        pass


def serve(handler):
    srv = HTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, f"http://127.0.0.1:{srv.server_port}/x"


from toolbox.download import fetch
import toolbox.download as dl
dl._hdrs = lambda: {}        # 本机无 bilibili_cookies.txt，单测不需要真实头

tmp = Path(tempfile.gettempdir())

# 1) 正常 206 续传：预置半截文件
srv, url = serve(RangeOK)
try:
    d = tmp / "_t_fetch_a.bin"
    d.write_bytes(BODY[:400_000])
    fetch(url, d)
    assert d.read_bytes() == BODY, "206 续传结果损坏"
    print("fetch 206 续传 ✓（预置 400KB 残段 + 服务器回 206）")
finally:
    srv.shutdown()

# 2) 服务器忽略 Range 回 200：旧实现会拼出 残段+全量 坏文件，新实现应重下
srv, url = serve(Always200)
try:
    d = tmp / "_t_fetch_b.bin"
    d.write_bytes(BODY[:400_000])         # 残段
    fetch(url, d)
    assert d.read_bytes() == BODY, "200 回退重下结果损坏"
    print("fetch 200 回退重下 ✓（残段被清空，不再拼接坏文件）")
finally:
    srv.shutdown()
    d.unlink(missing_ok=True)
    (tmp / "_t_fetch_a.bin").unlink(missing_ok=True)
print("ALL FETCH TESTS PASSED")
