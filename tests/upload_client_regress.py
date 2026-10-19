# -*- coding: utf-8 -*-
"""HTTPS 上传客户端回归（契约 1.3.0 C2S-6~9，REQ-20261019-005）。

用进程内 ThreadingHTTPServer 假服务端（与 upload_api.py 同一套语义）实测：
- 正常流：create → 分块 → complete 回执 state=imported；
- 断点续传：崩溃后同清单复用 upload_id，跳过已收字节；
- 409 实时对齐：服务端"收到块但响应丢失"（首块 409），端侧按 status 对齐续传；
- 422 checksum_mismatch：端侧重传出错文件后 complete 成功；
- rsync 排除语义等价：_excluded 尾斜杠段名 / basename 通配。
"""
import hashlib
import json
import re
import sys
import tempfile
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, ".")

from toolbox import transfer, uploader


class FakeBackend:
    """与 upload_api.py 同语义的最小内存后端。"""

    TOKEN = "test-token"
    CHUNK_MAX = 64 * 1024 * 1024

    def __init__(self, root: Path):
        self.root = root
        self.sessions = {}          # upload_id -> {files, received[], state, tenant}
        self.n = 0
        self.fail_first_chunk_409 = False
        self.fail_sha_idx = -1      # 首次 complete 对该文件回 422（服务端清零）

    def handle(self, h: BaseHTTPRequestHandler):
        u = urllib.parse.urlparse(h.path)
        if u.path == "/api/production/v1/capabilities":
            return {"upload_contract": "1.3.0", "chunk_max": self.CHUNK_MAX}
        auth = h.headers.get("Authorization", "")
        if auth != f"Bearer {self.TOKEN}":
            return _err(h, 401, "invalid_token", "坏 token")
        body = h.rfile.read(int(h.headers.get("Content-Length", 0))) \
            if h.command in ("POST", "PUT") else b""
        m = re.fullmatch(r"/api/production/v1/uploads(?:/([A-Za-z0-9_-]+)"
                         r"(?:/files/(\d+)|/complete)?)?", u.path)
        if not m:
            return _err(h, 404, "not_found", u.path)
        uid, idx = m.group(1), m.group(2)
        if h.command == "POST" and uid is None:
            req = json.loads(body)
            for f in req["files"]:
                if ".." in f["path"].split("/"):
                    return _err(h, 400, "invalid_path", f["path"])
            self.n += 1
            uid = f"up{self.n}"
            self.sessions[uid] = {
                "files": req["files"], "received": [0] * len(req["files"]),
                "state": "open", "tenant": "test_007",
                "dir": self.root / uid}
            self.sessions[uid]["dir"].mkdir(parents=True)
            return {"upload_id": uid, "tenant": "test_007"}
        sess = self.sessions.get(uid or "")
        if not sess:
            return _err(h, 404, "not_found", uid)
        if h.command == "GET":
            return {"state": sess["state"], "files": [
                {"path": f["path"], "size": f["size"], "received": r}
                for f, r in zip(sess["files"], sess["received"])]}
        if h.command == "PUT":
            i = int(idx)
            q = urllib.parse.parse_qs(u.query)
            offset = int(q["offset"][0])
            cur = sess["received"][i]
            if offset != cur:
                return _err(h, 409, "offset_mismatch", f"{offset}!={cur}")
            if self.fail_first_chunk_409 and cur == 0 and body:
                # 模拟"块已落盘但响应丢失"：先追加再回 409
                (sess["dir"] / str(i)).open("ab").write(body)
                sess["received"][i] += len(body)
                self.fail_first_chunk_409 = False
                return _err(h, 409, "offset_mismatch", "lost-response")
            with (sess["dir"] / str(i)).open("ab") as f:
                f.write(body)
            sess["received"][i] += len(body)
            return {"path": sess["files"][i]["path"],
                    "received": sess["received"][i]}
        if h.command == "POST":                        # complete
            req = json.loads(body) if body else {}
            if "manifest" not in req:
                return _err(h, 400, "invalid_request", "缺 manifest 请求体")
            sess["manifest"] = req["manifest"]
            for i, (f, r) in enumerate(zip(sess["files"], sess["received"])):
                if r != f["size"]:
                    return _err(h, 409, "state_conflict", f["incomplete {i}"])
                digest = hashlib.sha256(
                    (sess["dir"] / str(i)).read_bytes()).hexdigest()
                if i == self.fail_sha_idx or digest != f["sha256"]:
                    sess["received"][i] = 0            # 服务端清零重传
                    (sess["dir"] / str(i)).unlink()
                    self.fail_sha_idx = -1
                    return _err(h, 422, "checksum_mismatch",
                                f["path"], extra={"index": i})
            sess["state"] = "imported"
            return {"state": "imported", "upload_id": uid,
                    "session": "s1", "tenant": "test_007",
                    "remapped": 0, "video_ready": True, "log": "import ok"}
        return _err(h, 404, "not_found", h.command)


def _err(h, status, code, message, extra=None):
    h.send_response(status)
    body = json.dumps({"error": {"code": code, "message": message,
                                 **(extra or {})}}, ensure_ascii=False)
    h.send_header("Content-Type", "application/json")
    h.send_header("Content-Length", str(len(body.encode())))
    h.end_headers()
    h.wfile.write(body.encode())
    return None


def make_server(backend):
    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _run(self):
            try:
                r = backend.handle(self)
            except Exception as e:                     # noqa: BLE001
                r = _err(self, 500, "internal", str(e))
            if r is not None:
                body = json.dumps(r, ensure_ascii=False).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        do_GET = do_POST = do_PUT = _run

    srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def make_files(td: Path, sizes):
    out = []
    for name, size in sizes.items():
        p = td / name
        p.parent.mkdir(parents=True, exist_ok=True)
        blob = (f"{name}-").encode() * (size // (len(name) + 1) + 1)
        p.write_bytes(blob[:size])
        out.append((name, p.stat().st_size, uploader.file_sha256(p)))
    return out


def main():
    ok = 0

    def check(cond, what):
        nonlocal ok
        assert cond, what
        ok += 1
        print(f"  ✓ {what}")

    with tempfile.TemporaryDirectory() as td:
        t = Path(td)
        backend = FakeBackend(t / "staging")
        srv = make_server(backend)
        base = f"http://127.0.0.1:{srv.server_port}/api/production/v1"

        # 假 DATA 根：.uploads.json 会写到 DATA 下
        real_data = uploader.DATA
        uploader.DATA = t / "data"
        real_rep = transfer.REPORTS
        transfer.REPORTS = t / "data" / "reports"
        try:
            up = uploader.ProductionUploader(base, FakeBackend.TOKEN,
                                             chunk=1024, echo=lambda *a, **k: None)
            files = make_files(t / "src", {"a.bin": 2500, "b.bin": 100})
            local_map = {n: t / "src" / n for n, _, _ in files}
            man = {"contract_version": "1.3.0", "session": "s1",
                   "mode": "eval", "parts": [], "missing": [], "lines": {}}

            # 1) 正常流
            r = uploader.push_files(up, "s1", "eval", files, local_map, man)
            check(r["state"] == "imported", "正常流 complete state=imported")
            check(backend.sessions[r["upload_id"]].get("manifest") == man,
                  "manifest 经 complete 请求体送达")
            check(backend.sessions[r["upload_id"]]["received"]
                  == [f[1] for f in files], "服务端逐文件字节收齐")

            # 2) 崩溃续传：新 uploader 进程模拟（同清单同 registry）
            backend2 = FakeBackend(t / "st2")
            srv2 = make_server(backend2)
            base2 = f"http://127.0.0.1:{srv2.server_port}/api/production/v1"
            up2 = uploader.ProductionUploader(base2, FakeBackend.TOKEN,
                                              chunk=1024, echo=lambda *a, **k: None)
            files2 = make_files(t / "src2", {"big.bin": 5000})
            local_map2 = {n: t / "src2" / n for n, _, _ in files2}
            # 先手工传一半再"崩溃"
            half = uploader.push_files  # noqa: F841（可读性）
            r2 = up2.create("s2", "eval", files2)
            uid2 = r2["upload_id"]
            up2.upload_file(uid2, 0, local_map2["big.bin"], files2[0][1])
            st = up2.status(uid2)
            check(st["files"][0]["received"] == 5000, "首轮全量上传完成")
            # 清零模拟崩溃：服务端留 2 块（2048B）
            backend2.sessions[uid2]["received"][0] = 2048
            (backend2.sessions[uid2]["dir"] / "0").write_bytes(
                (t / "src2" / "big.bin").read_bytes()[:2048])
            uploader.save_upload_id("s2", "eval",
                                    uploader.files_signature(files2), uid2)
            r2 = uploader.push_files(up2, "s2", "eval", files2, local_map2,
                                     {**man, "session": "s2"})
            check(r2["state"] == "imported", "续传后 complete 成功")
            got = (backend2.sessions[uid2]["dir"] / "0").read_bytes()
            check(got == (t / "src2" / "big.bin").read_bytes(),
                  "续传内容与源文件一致")

            # 3) 409 对齐（服务端收到块但响应 409）
            backend3 = FakeBackend(t / "st3")
            srv3 = make_server(backend3)
            up3 = uploader.ProductionUploader(
                f"http://127.0.0.1:{srv3.server_port}/api/production/v1",
                FakeBackend.TOKEN, chunk=1024, echo=lambda *a, **k: None)
            backend3.fail_first_chunk_409 = True
            files3 = make_files(t / "src3", {"c.bin": 3000})
            r3 = uploader.push_files(up3, "s3", "eval", files3,
                                     {"c.bin": t / "src3" / "c.bin"},
                                     {**man, "session": "s3"})
            check(r3["state"] == "imported", "409 对齐后仍能 complete")
            sess3 = backend3.sessions[r3["upload_id"]]
            check((sess3["dir"] / "0").read_bytes()
                  == (t / "src3" / "c.bin").read_bytes(), "409 场景内容一致")

            # 4) 422 重传（首次 complete 校验失败 → 清零 → 端侧重传）
            backend4 = FakeBackend(t / "st4")
            srv4 = make_server(backend4)
            up4 = uploader.ProductionUploader(
                f"http://127.0.0.1:{srv4.server_port}/api/production/v1",
                FakeBackend.TOKEN, chunk=1024, echo=lambda *a, **k: None)
            backend4.fail_sha_idx = 0
            files4 = make_files(t / "src4", {"d.bin": 1500, "e.bin": 200})
            r4 = uploader.push_files(up4, "s4", "eval", files4,
                                     {n: t / "src4" / n for n, _, _ in files4},
                                     {**man, "session": "s4"})
            check(r4["state"] == "imported", "422 重传后 complete 成功")

            # 5) rsync 排除语义等价
            ex = transfer._excluded
            check(ex("x/audio_16k.wav", ("audio_16k.wav", "*.log")),
                  "basename 精确名排除")
            check(ex("x/sub/a.log", ("audio_16k.wav", "*.log")),
                  "basename 通配排除")
            check(ex("x/l2/a.json", ("l2/",)), "目录段名（尾斜杠）排除")
            check(ex("l2/b.json", ("l2/",)), "根段就是 l2/ 也排除")
            check(not ex("xl2/a.json", ("l2/",)), "段名前缀不算命中")
            check(not ex("a/audio_16k.wav.bak", ("audio_16k.wav",)),
                  "通配不含扩展外后缀")

            # 6) token 无效 → UploadError(code=invalid_token)
            bad = uploader.ProductionUploader(base, "wrong",
                                              echo=lambda *a, **k: None)
            try:
                bad.create("s9", "eval", [("p", 1, "0" * 64)])
                raise AssertionError("坏 token 应 401")
            except uploader.UploadError as e:
                check(e.code == "invalid_token" and e.status == 401,
                      "坏 token → 401 invalid_token")
        finally:
            uploader.DATA = real_data
            transfer.REPORTS = real_rep
            srv.shutdown()
    print(f"PASS: {ok} 项断言全部通过")


if __name__ == "__main__":
    main()
