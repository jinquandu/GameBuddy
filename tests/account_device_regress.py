# -*- coding: utf-8 -*-
"""设备身份 + 统一账号客户端回归（契约 §4A / DSJ-PC-1 原始规范，REQ-002）。

进程内 mock 账号服务按契约语义实现：能力门控（health 声明）、challenge
两步登录验签（8 行规范化串、五签名头、公钥随头、device_id=摘要无前缀）、
绑定后所有请求强制验签+Bearer、device_bound 换机流、生产申请/任务查询/
产物读取。另验：密钥 DPAPI 落盘非明文、会话落盘非明文、篡改 body 验签
失败、uploader 鉴权钩子携带签名头。
"""
import base64
import hashlib
import json
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, ".")

from toolbox import account, device_identity as di, uploader

NL = chr(10)


def make_mock_server(state):
    """state: {pc_auth:bool, bound:dict, bound_device:str|None, jobs:dict}"""

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _json(self, obj, status=200):
            body = json.dumps(obj, ensure_ascii=False).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _err(self, status, code, message, **extra):
            self._json({"error": {"code": code, "message": message, **extra}},
                       status)

        def _read(self):
            n = int(self.headers.get("Content-Length", 0))
            return self.rfile.read(n) if n else b""

        def _verify_sig(self, body, bearer, pk_override=None):
            """绑定后强制验签（DSJ-PC-1 原始规范：8 行串 path 含 query、五头）。"""
            hl = {k.lower(): v for k, v in self.headers.items()}
            dev = hl.get("x-dsj-device-id", "")
            key_b64 = hl.get("x-dsj-device-key", "")
            if pk_override is not None:
                pk = pk_override
            else:
                if not key_b64:
                    return False
                pk = base64.urlsafe_b64decode(
                    key_b64 + "=" * (-len(key_b64) % 4))
                if hashlib.sha256(pk).hexdigest()[:32] != dev:
                    return False
                if not state["bound"].get(dev):
                    return False
            payload = NL.join([
                "DSJ-PC-1", self.command, self.path,
                hashlib.sha256(body or b"").hexdigest(),
                hashlib.sha256((bearer or "").encode()).hexdigest(),
                dev, hl.get("x-dsj-device-time", ""),
                hl.get("x-dsj-device-nonce", "")]).encode()
            try:
                sig_b64 = hl.get("x-dsj-device-signature", "")
                sig = base64.urlsafe_b64decode(
                    sig_b64 + "=" * (-len(sig_b64) % 4))
                from cryptography.hazmat.primitives.asymmetric.ed25519 \
                    import Ed25519PublicKey
                Ed25519PublicKey.from_public_bytes(pk).verify(sig, payload)
                return True
            except Exception:
                return False

        def _bearer_ok(self):
            a = self.headers.get("Authorization", "")
            return a == "Bearer " + state.get("issued_bearer", "")

        def _run(self):
            u_path = self.path.split("?")[0]
            body = self._read()
            if u_path == "/api/account/v1/health":
                out = {"status": "ok"}
                if state["pc_auth"]:
                    out["pc_device_auth_contract"] = "1.0"
                return self._json(out)
            if not state["pc_auth"]:
                return self._err(503, "dependency_unavailable", "未升级")
            if u_path == "/api/account/v1/sms":
                for h in ("X-DSJ-Device-Id", "X-DSJ-Device-Signature"):
                    if not self.headers.get(h):
                        return self._err(401, "invalid_token", "缺 " + h)
                return self._json({"sent": True, "challenge_id": "ch-1"})
            if u_path == "/api/account/v1/login":
                req = json.loads(body)
                hl = {k.lower(): v for k, v in self.headers.items()}
                key_b64 = hl.get("x-dsj-device-key", "")
                if not key_b64:
                    return self._err(400, "invalid_request", "缺公钥头")
                pk = base64.urlsafe_b64decode(
                    key_b64 + "=" * (-len(key_b64) % 4))
                expect_dev = hashlib.sha256(pk).hexdigest()[:32]
                if req["device_id"] != expect_dev:
                    return self._err(400, "invalid_request", "device_id 摘要不符")
                if req.get("challenge_id") != "ch-1":
                    return self._err(400, "invalid_request", "challenge 不符")
                if not self._verify_sig(body, "", pk_override=pk):
                    return self._err(401, "invalid_token", "登录签名验证失败")
                if state["bound_device"] and state["bound_device"] != expect_dev \
                        and not req.get("replace_device"):
                    return self._err(409, "device_bound",
                                     "已绑定其他设备", bound_device={"digest": "…"})
                state["bound_device"] = expect_dev
                state["bound"][expect_dev] = pk
                state["issued_bearer"] = "tok-u1"
                return self._json({"access_token": "tok-u1", "expires_in": 86400,
                                   "user": {"user_id": "u1",
                                            "phone": "138****0000"}})
            if not self._bearer_ok():
                return self._err(401, "invalid_token", "会话无效")
            if not self._verify_sig(body, state["issued_bearer"]):
                return self._err(401, "auth_replay", "签名无效")
            if u_path == "/api/account/v1/me":
                return self._json({"user_id": "u1", "phone": "138****0000"})
            if u_path == "/api/account/v1/logout":
                return self._json({"ok": True})
            if u_path == "/api/account/v1/production-jobs":
                req = json.loads(body)
                jid = "job-" + req["request_id"][:8]
                state["jobs"][jid] = req
                return self._json({"job_id": jid, "status": "queued"})
            if u_path.startswith("/api/production/v1/jobs/"):
                parts = u_path.split("/")
                jid = parts[5] if len(parts) > 5 else ""
                if len(parts) == 8 and parts[6] == "artifacts":
                    if jid not in state["jobs"]:
                        return self._err(404, "not_found", "job 不存在")
                    data = b"FAKE-VIDEO-BYTES"
                    self.send_response(200)
                    self.send_header("Content-Type", "video/mp4")
                    self.send_header("Content-Length", str(len(data)))
                    self.end_headers()
                    return self.wfile.write(data)
                if jid not in state["jobs"]:
                    return self._err(404, "not_found", "job 不存在")
                return self._json({"job_id": jid, "status": "done",
                                   "artifacts": [{"file_id": "f1",
                                                  "kind": "video",
                                                  "name": "out.mp4"}]})
            return self._err(404, "not_found", u_path)

        do_GET = do_POST = do_PUT = _run

    srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def main():
    ok = 0

    def check(cond, what):
        nonlocal ok
        assert cond, "FAIL: " + what
        ok += 1
        print("  ✓ " + what)

    with tempfile.TemporaryDirectory() as td:
        t = Path(td)
        real = (di.DEVICE_DIR, di.KEY_FILE, di.SESSION_FILE, di._KEY)
        di.DEVICE_DIR = t / ".device"
        di.KEY_FILE = di.DEVICE_DIR / "pc-device-v1.dpapi"
        di.SESSION_FILE = di.DEVICE_DIR / "session.json"
        di._KEY = None
        try:
            # 1) 设备身份
            dev1 = di.device_id()
            blob = di.KEY_FILE.read_bytes()
            check(len(dev1) == 32 and di.device_code().startswith("DSJ-"),
                  "device_id=32hex 无前缀；展示码 DSJ-8x4")
            check(blob[:4] in (b"DP01", b"DP00"), "密钥带封装头")
            if blob[:4] == b"DP01":
                seed = di.device_key().private_bytes_raw()
                check(seed not in blob, "DPAPI 落盘无私钥明文")
            di._KEY = None
            check(di.device_id() == dev1, "重载密钥 device_id 恒定")

            url = "/api/production/v1/uploads/x/files/0?offset=32"
            hdrs = di.sign_request("PUT", url, b"chunk-bytes", bearer="tok-u1")
            check(all(hdrs.get(k) for k in
                      ("X-DSJ-Device-Id", "X-DSJ-Device-Key",
                       "X-DSJ-Device-Time", "X-DSJ-Device-Nonce",
                       "X-DSJ-Device-Signature")), "签名头五项齐全(含公钥头)")
            check(di.verify_request(hdrs, "PUT", url, b"chunk-bytes",
                                    "tok-u1", di.public_key_bytes()),
                  "规范化串验签通过(8行 path 含 query)")
            check(not di.verify_request(hdrs, "PUT", url, b"chunk-TAMPERED",
                                        "tok-u1", di.public_key_bytes()),
                  "篡改 body 验签拒绝")

            # 2) 账号客户端（mock）
            state = {"pc_auth": False, "bound": {}, "bound_device": None,
                     "jobs": {}}
            srv = make_mock_server(state)
            base = "http://127.0.0.1:" + str(srv.server_port)
            cli = account.AccountClient(base + "/api/account/v1")

            try:
                cli.request_sms("13800000000")
                raise AssertionError("未就绪应抛 ServiceNotReady")
            except account.ServiceNotReady as e:
                check(e.code == "service_not_ready",
                      "能力门控:health 未声明→ServiceNotReady")
            try:
                cli.login("ch-1", "1234")
                raise AssertionError("未就绪登录应被拒")
            except account.ServiceNotReady:
                check(True, "能力门控:未就绪时 login 拒绝(不降级不绕过)")

            state["pc_auth"] = True
            r = cli.request_sms("13800000000")
            check(r.get("challenge_id") == "ch-1", "sms 返回 challenge_id")
            sess = cli.login(r["challenge_id"], "1234", phone="13800000000")
            check(sess["user_id"] == "u1" and sess["bearer"] == "tok-u1",
                  "C2S-10 challenge 两步登录成功(公钥走签名头)")
            sblob = di.SESSION_FILE.read_bytes()
            check(b"tok-u1" not in sblob, "会话落盘无 bearer 明文(DPAPI)")
            check(cli.me().get("user_id") == "u1",
                  "me 签名+Bearer 通过(绑定后强制验签)")
            check(cli.session_summary().get("expires_in", 0) > 0,
                  "session_summary 状态行")

            # 换机流：另一设备已绑 → 409 device_bound
            state2 = {"pc_auth": True, "bound": {}, "bound_device": "otherdevice",
                      "jobs": {}}
            srv2 = make_mock_server(state2)
            cli2 = account.AccountClient(
                "http://127.0.0.1:" + str(srv2.server_port) + "/api/account/v1")
            try:
                cli2.login("ch-1", "1234")
                raise AssertionError("应 409 device_bound")
            except account.AccountError as e:
                check(e.code == "device_bound" and e.status == 409,
                      "换机未确认 → 409 device_bound(不自动抢占)")
                rr = cli2.login("ch-1", "1234", replace_device=True)
                check(rr["user_id"] == "u1", "replace_device=true 换绑成功")
            di.SESSION_FILE.unlink(missing_ok=True)      # cli2 会话清掉
            di.save_session(sess)                        # 恢复 cli1 会话

            # 生产线 C2S-12~14
            r = cli.production_job("upl-abc", "req-uuid-1")
            check(r["job_id"].startswith("job-") and r["status"] == "queued",
                  "C2S-12 生产申请受理")
            st = cli.job_status(r["job_id"])
            check(st["status"] == "done" and st["artifacts"][0]["file_id"] == "f1",
                  "C2S-13 任务查询(最小 schema)")
            data = cli.open_artifact(r["job_id"], "f1")
            check(data == b"FAKE-VIDEO-BYTES", "C2S-14 产物字节回流")
            try:
                cli.job_status("job-nope")
                raise AssertionError("不存在 job 应 404")
            except account.AccountError as e:
                check(e.code == "not_found" and e.status == 404,
                      "他人/不存在 job → 404 同形")

            # 3) uploader 鉴权钩子（1.4.0 账号模式挂接点）
            req_seen = {}

            class H2(BaseHTTPRequestHandler):
                def log_message(self, *a2):
                    pass

                def do_GET(self):
                    req_seen.update(dict(self.headers.items()))
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.end_headers()
                    self.wfile.write(b"{}")

            srv3 = ThreadingHTTPServer(("127.0.0.1", 0), H2)
            threading.Thread(target=srv3.serve_forever, daemon=True).start()
            up3 = uploader.ProductionUploader(
                "http://127.0.0.1:" + str(srv3.server_port)
                + "/api/production/v1",
                signer=lambda m, p, b: di.sign_request(m, p, b, bearer="t"))
            up3._request("GET", "/capabilities")
            hl = {k.lower() for k in req_seen}     # HTTP 头名大小写不敏感
            check("x-dsj-device-signature" in hl and "authorization" not in hl,
                  "uploader 钩子:签名头注入、无 token 时不带 Bearer")

            cli.logout()
            check(cli.session_summary() == {}, "logout 清会话")
        finally:
            di.DEVICE_DIR, di.KEY_FILE, di.SESSION_FILE, di._KEY = real
            di._KEY = None
    print("PASS: " + str(ok) + " 项断言全部通过")


if __name__ == "__main__":
    main()
