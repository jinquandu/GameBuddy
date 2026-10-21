# -*- coding: utf-8 -*-
"""微信绑定三前置回归（REQ-20261021-001 规则1/方案A；S1 端点）。

进程内 mock 账号服务：health 双能力声明（pc_device_auth_contract +
pc_binding_contract）、wechat-binding 状态查询（bound=identity_verified∧
relation_active，与通知开关分离）、配对 create/lookup/confirm/cancel
（状态机由测试侧推动）、/me 兼容字段（entitlements/binding 可缺省容错）、
C2S-12 403 binding_required。覆盖 L1 铁律：未知（服务端未返回/网络失败）
不拦截、不缓存；仅「明确未绑定」拦下并给出引导。
"""
import json
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, ".")

from toolbox import account, device_identity as di


def make_mock_server(state):
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

        def _err(self, status, code, message):
            self._json({"error": {"code": code, "message": message}}, status)

        def _bearer_ok(self):
            return self.headers.get("Authorization", "") == "Bearer tok-u1"

        def _run(self):
            p = self.path.split("?")[0]
            n = int(self.headers.get("Content-Length", 0))
            self.rfile.read(n) if n else b""
            if p == "/api/account/v1/health":
                out = {"status": "ok", "pc_device_auth_contract": "1.0"}
                if state.get("binding_contract"):
                    out["pc_binding_contract"] = "1.3.0"
                return self._json(out)
            if not self._bearer_ok() and p != "/api/account/v1/health":
                return self._err(401, "invalid_token", "会话无效")
            if p == "/api/account/v1/me":
                return self._json(state.get("me")
                                  or {"user_id": "u1", "phone": "138****0000"})
            if p == "/api/account/v1/wechat-binding":
                if state.get("binding_500"):
                    return self._err(500, "internal", "抖动")
                return self._json({"binding": state.get("binding")})
            if p == "/api/account/v1/wechat-binding/requests" \
                    and self.command == "POST":
                state["pairing"] = {
                    "request_id": "r" * 32, "state": "pending",
                    "pairing_code": "482913",
                    "expires_at": time.time() + 300,
                    "scan_url": "https://h5.example/binding/pc/#ticket=t"}
                return self._json(state["pairing"], 201)
            if p.startswith("/api/account/v1/wechat-binding/requests/"):
                # /requests/<rid>[/confirm|/cancel]
                parts = p.split("/")
                rid, action = parts[6], (parts[7] if len(parts) > 7 else "")
                pair = state.get("pairing") or {}
                if rid != pair.get("request_id"):
                    return self._err(404, "not_found", "配对不存在")
                if self.command == "GET":
                    return self._json({k: v for k, v in pair.items()
                                       if k != "ticket"})
                if action == "confirm":
                    if pair.get("state") != "awaiting_pc":
                        return self._err(409, "conflict", "未完成核验")
                    state["pairing"]["state"] = "confirmed"
                    state["binding"] = {"identity_verified": True,
                                        "relation_active": True,
                                        "needs_reconciliation": False,
                                        "notification_opt_in": True}
                    return self._json({"state": "confirmed"})
                if action == "cancel":
                    state["pairing"]["state"] = "cancelled"
                    return self._json({"state": "cancelled"})
                return self._err(404, "not_found", "未知动作")
            if p == "/api/account/v1/production-jobs":
                if state.get("reject_binding"):
                    return self._err(403, "binding_required", "微信未绑定")
                return self._json({"job_id": "job-1", "status": "queued"})
            return self._err(404, "not_found", "未知路径")

        do_GET = do_POST = lambda self: self._run()

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
            state = {"binding_contract": True, "binding": None}
            srv = make_mock_server(state)
            cli = account.AccountClient(
                "http://127.0.0.1:" + str(srv.server_port)
                + "/api/account/v1")

            # 能力门控：未声明 pc_binding_contract → 不降级
            state["binding_contract"] = False
            try:
                cli.ensure_binding_ready()
                raise AssertionError("未声明应拒绝")
            except account.AccountError as e:
                check(e.code == "service_not_ready",
                      "绑定能力门控:health 未声明→service_not_ready")
            state["binding_contract"] = True
            check(cli.ensure_binding_ready().get("pc_binding_contract")
                  == "1.3.0", "绑定能力门控:声明 1.3.0 放行")

            # 未登录 → 绑定线一律 invalid_token（不降级匿名查）
            try:
                cli.wechat_binding()
                raise AssertionError("未登录应拒绝")
            except account.AccountError as e:
                check(e.code == "invalid_token", "未登录:绑定查询拒绝")
            check(cli.prerequisites() == {"logged_in": False, "member": None,
                                          "binding": None},
                  "prerequisites:未登录仅本地可判项")

            di.save_session({"user_id": "u1", "bearer": "tok-u1",
                             "expires_at_ts": time.time() + 3600})

            # wechat_binding 状态推导（绑定与通知开关分离）
            r = cli.wechat_binding()
            check(r["state"] == "none" and r["opt_in"] is None,
                  "wechat-binding:binding=null → none")
            state["binding"] = {"identity_verified": True,
                                "relation_active": False,
                                "needs_reconciliation": False,
                                "notification_opt_in": True}
            check(cli.wechat_binding()["state"] == "none",
                  "wechat-binding:关系失效 → none(通知开关无关)")
            state["binding"]["relation_active"] = True
            r = cli.wechat_binding()
            check(r["state"] == "bound" and r["opt_in"] is True,
                  "wechat-binding:核验+关系有效 → bound")

            # 配对流 create/lookup/confirm/cancel
            r = cli.binding_request()
            check(r["scan_url"].startswith("https://h5.example/binding/pc/")
                  and r["pairing_code"] == "482913" and r["state"] == "pending",
                  "binding_request:scan_url+配对码+pending")
            state["pairing"]["state"] = "awaiting_pc"
            check(cli.binding_lookup(r["request_id"])["state"]
                  == "awaiting_pc", "binding_lookup:awaiting_pc")
            try:
                cli.binding_confirm(r["request_id"])   # 已 awaiting → 过
            except account.AccountError:
                raise AssertionError("awaiting_pc 应可确认")
            check(cli.wechat_binding()["state"] == "bound",
                  "confirm 落绑定后现查 bound")
            r2 = cli.binding_request()
            state["pairing"]["state"] = "cancelled"
            check(cli.binding_cancel(r2["request_id"])["state"] == "cancelled",
                  "binding_cancel:pending 作废")

            # /me 兼容字段容错（2026-10-21 补录:可缺省 = 未知 None）
            s = cli.me_summary()
            check(s["membership_active"] is None and s["binding_state"] is None,
                  "me_summary:无 entitlements/binding → None(不臆断)")
            state["me"] = {"user_id": "u1", "phone": "138****0000",
                           "entitlements": {"membership_active": False,
                                            "membership_expires_at": None},
                           "binding": {"state": "none"}}
            s = cli.me_summary()
            check(s["membership_active"] is False and s["binding_state"]
                  == "none", "me_summary:扩展字段如实解析(含 False)")

            # prerequisites:/me 未扩展 → 回落既有 wechat-binding 端点
            state["me"] = None
            state["binding"] = None
            pre = cli.prerequisites()
            check(pre == {"logged_in": True, "member": None,
                          "binding": "none"},
                  "prerequisites:回落 wechat-binding → none")
            state["binding_500"] = True                  # 网络失败 → 未知不拦
            pre = cli.prerequisites()
            check(pre["binding"] is None and pre["member"] is None,
                  "prerequisites:服务异常 → None(L1 未知不拦截)")
            state["binding_500"] = False

            # C2S-12 binding_required → 引导文案（方案A:只引导不重试）
            state["reject_binding"] = True
            try:
                cli.production_job("upl-1", "req-1")
                raise AssertionError("未绑定应 403")
            except account.AccountError as e:
                check(e.code == "binding_required" and e.status == 403,
                      "C2S-12:未绑定 → 403 binding_required")
                hint = account.binding_hint(e)
                check(hint and "去绑定" in hint,
                      "binding_hint:引导文案")
            check(account.binding_hint(
                account.AccountError("no_entitlement", "x", 403)) is None,
                "binding_hint:其他错误码不误引导")
        finally:
            di.DEVICE_DIR, di.KEY_FILE, di.SESSION_FILE, di._KEY = real
            di._KEY = None
    print("PASS: " + str(ok) + " 项断言全部通过")


if __name__ == "__main__":
    main()
