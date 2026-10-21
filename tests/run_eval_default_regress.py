# -*- coding: utf-8 -*-
"""REQ-20261021-002 回归：评估包 push 默认触发服务端评估链（run_eval）。

事故根因：后台日任务链（folder_watch→pipeline）与 pet 手动链均未透传
run_eval（缺省 False），评估包上传后服务端只 import 不评分，打标站
0 片段、均须人工补跑 eval。修复语义：

- CLI push/pipeline 两子命令缺省 run_eval=True；--run-eval 显式确认
  仍有效；--no-run-eval 为逃生门（服务端调试）；
- transfer.push / pipeline.run_pipeline 默认值翻转为 True；
- push_files 模式门：video 包 create/complete 请求体恒 run_eval=false
  （REQ 应用端改动 2：video 模式行为不变）。
"""
import inspect
import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, ".")

from toolbox import cli, pipeline, transfer, uploader


def make_capture_server():
    """最小上传后端：捕获 create/complete 请求体的 run_eval 字段。"""
    seen = {"create": [], "complete": []}

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _run(self):
            p = self.path.split("?")[0]
            n = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(n)) if n else {}

            def out(obj, status=200):
                b = json.dumps(obj).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(b)))
                self.end_headers()
                self.wfile.write(b)

            if p == "/api/production/v1/capabilities":
                return out({"upload_contract": "1.3.0",
                            "chunk_max": 67108864})
            if p == "/api/production/v1/uploads" and self.command == "POST":
                seen["create"].append(body.get("run_eval"))
                return out({"upload_id": "u1", "tenant": "t"}, 201)
            if p == "/api/production/v1/uploads/u1" and self.command == "GET":
                return out({"state": "open", "files": []})
            if p.endswith("/complete") and self.command == "POST":
                seen["complete"].append(body.get("run_eval"))
                return out({"state": "imported", "session": "s",
                            "tenant": "t", "remapped": 0})
            return out({"error": {"code": "not_found", "message": p}}, 404)

        do_GET = do_POST = do_PUT = lambda self: self._run()

    srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, seen


def main():
    ok = 0

    def check(cond, what):
        nonlocal ok
        assert cond, "FAIL: " + what
        ok += 1
        print("  ✓ " + what)

    # 1) CLI 缺省与旗标（push / pipeline 两入口）
    p = cli.build_parser()
    check(p.parse_args(["push", "s1"]).run_eval is True,
          "CLI push 缺省 run_eval=True（默认触发评估链）")
    check(p.parse_args(["push", "s1", "--run-eval"]).run_eval is True,
          "CLI push --run-eval 显式确认仍有效（兼容既有脚本）")
    check(p.parse_args(["push", "s1", "--no-run-eval"]).run_eval is False,
          "CLI push --no-run-eval 逃生门")
    check(p.parse_args(["pipeline", "addr"]).run_eval is True,
          "CLI pipeline 缺省 run_eval=True（后台日任务链走此默认）")
    check(p.parse_args(["pipeline", "addr", "--no-run-eval"]).run_eval
          is False, "CLI pipeline --no-run-eval 逃生门")
    check(p.parse_args(["pipeline", "addr", "--run-eval"]).run_eval is True,
          "CLI pipeline --run-eval 兼容")

    # 2) 层默认值（后台链 folder_watch 不传参 → 走 run_pipeline 默认）
    check(inspect.signature(transfer.push).parameters["run_eval"].default
          is True, "transfer.push 默认 run_eval=True")
    check(inspect.signature(pipeline.run_pipeline).parameters["run_eval"]
          .default is True, "run_pipeline 默认 run_eval=True")

    # 3) push_files 模式门（create/complete 请求体逐字验证）
    import uuid
    srv, seen = make_capture_server()
    base = "http://127.0.0.1:" + str(srv.server_port) + "/api/production/v1"
    up = uploader.ProductionUploader(base, "test-token")
    for mode, flag, want, what in (
            ("eval", True, True,
             "eval+True(transfer.push 新默认) → create/complete 均 true"),
            ("eval", False, False,
             "eval+显式 False(--no-run-eval) → false"),
            ("video", True, False,
             "video+True → 恒 false（模式门，video 永不触发评估）")):
        # 随机 session 名：save_upload_id 状态跨运行持久化,固定名会命中
        # 上次运行的续传缓存而跳过 create,污染断言
        uploader.push_files(up, f"rg-{mode}-{uuid.uuid4().hex[:8]}", mode,
                            [], {}, {}, run_eval=flag)
        check(seen["complete"][-1] is want and seen["create"][-1] is want,
              f"push_files {what}")
    print("PASS: " + str(ok) + " 项断言全部通过")


if __name__ == "__main__":
    main()
