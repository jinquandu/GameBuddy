"""统一账号客户端（契约 §4A，1.4.0 冻结；C2S-10~14）。

能力门控（C2S-11）：`health` 未声明 `pc_device_auth_contract=1.0` → 一律
`ServiceNotReady`（UI 显示"服务待就绪"），**不降级、不绕过、不回退旧通道**
（REQ-002 B1/§4A 总则4）。登录/短信请求带 DSJ-PC-1 签名头；会话 bearer
经 device_identity 落 DPAPI。错误按机器码抛 AccountError（§4A 总则6）。

端侧在账号服务（REQ-003 S1）上线前即可合入本模块：所有入口先过能力门控，
服务未就绪时表现明确。生产任务查询（C2S-13/14）走 production_url
（缺省从 api_url 推导：/api/account/v1 → /api/production/v1）。
"""
import json
import time
import urllib.error
import urllib.parse
import urllib.request

from toolbox import device_identity as di

SUPPORTED_PC_AUTH = "1.0"


class AccountError(RuntimeError):
    def __init__(self, code, message, status=None, data=None):
        super().__init__(f"[{code}] {message}")
        self.code, self.status, self.data = code, status, (data or {})


class ServiceNotReady(AccountError):
    """账号服务未声明设备鉴权能力（C2S-11 门控未过）。"""

    def __init__(self, detail=""):
        super().__init__("service_not_ready",
                         f"账号服务暂不支持 PC 设备登录（{detail}）")


class NotLoggedIn(AccountError):
    """识别链路登录门控未过（REQ-20261019-013）。"""

    def __init__(self):
        super().__init__("not_logged_in",
                         "请先在桌宠界面登录后再跑识别链路", 401)


def require_login(config=None):
    """识别链路登录门控（REQ-20261019-013）。

    account.api_url 已配置（账号模式）且无有效会话 → NotLoggedIn；
    未配置账号服务（内部机）不门控；account.gate_pipeline=false 关闭
    （开发逃生门）。会话有效性与 AccountClient._bearer 同口径：
    bearer 存在且未过期；过期会话不在此清（_bearer/summary 自会清）。"""
    if config is None:
        from toolbox.config import load_config
        config = load_config()
    acct = config.get("account") or {}
    if not acct.get("api_url") or acct.get("gate_pipeline") is False:
        return {}
    sess = di.load_session()
    exp = sess.get("expires_at_ts")
    if sess.get("bearer") and exp and exp > time.time() + 30:
        return sess
    raise NotLoggedIn()


class AccountClient:
    def __init__(self, api_url, production_url=None):
        self.api = api_url.rstrip("/")
        self.prod = (production_url or api_url.replace(
            "/api/account/v1", "/api/production/v1")).rstrip("/")

    # ------------------------------------------------------------ HTTP 基座

    def _request(self, base, method, path, body=None, bearer="", timeout=30):
        data = json.dumps(body, ensure_ascii=False).encode("utf-8") \
            if body is not None else None
        headers = {"Content-Type": "application/json"} if data else {}
        if bearer:
            headers["Authorization"] = f"Bearer {bearer}"
        # DSJ-PC-1：所有账号/生产请求均带签名头；签「线上原样 path」=
        # base 的路径前缀 + 相对 path（服务端按收到的原样请求验签）
        wire_path = urllib.parse.urlsplit(base).path + path
        headers.update(di.sign_request(method, wire_path, data or b"", bearer))
        req = urllib.request.Request(base + path, data=data, headers=headers,
                                     method=method)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                raw = r.read()
                ct = r.headers.get("Content-Type", "")
                if "json" in ct:
                    return json.loads(raw.decode("utf-8"))
                return {"_raw": raw, "_status": r.status}
        except urllib.error.HTTPError as e:
            try:
                err = json.loads(e.read().decode("utf-8")).get("error", {})
            except Exception:
                err = {}
            raise AccountError(err.get("code", "http_error"),
                               err.get("message", f"HTTP {e.code}"),
                               e.code, err) from None
        except urllib.error.URLError as e:
            raise AccountError("network", f"网络错误：{e.reason}") from None

    def _bearer(self):
        sess = di.load_session()
        exp = sess.get("expires_at_ts")
        if sess.get("bearer") and exp and exp > time.time() + 30:
            return sess["bearer"], sess
        if sess.get("bearer"):
            di.clear_session()               # 过期会话不残留
        return "", {}

    # ------------------------------------------------------------ C2S-11 门控

    def health(self):
        """GET /api/account/v1/health（无签名要求也带，服务端可忽略）。"""
        return self._request(self.api, "GET", "/health")

    def ensure_pc_auth(self):
        """能力门控：未见 pc_device_auth_contract=1.0 → ServiceNotReady。
        未知/缺失能力必须拒绝走账号通道（§4A C2S-11，不沿用告警后继续）。"""
        h = self.health()
        v = h.get("pc_device_auth_contract")
        if v != SUPPORTED_PC_AUTH:
            raise ServiceNotReady(f"health 声明 {v!r}")
        return h

    # ------------------------------------------------------------ 登录线

    def request_sms(self, phone):
        self.ensure_pc_auth()                 # 发短信前先检查设备契约（REQ-002 B1）
        return self._request(self.api, "POST", "/sms", {"phone": phone})

    def login(self, challenge_id, code, phone="", replace_device=False):
        """C2S-10（challenge 两步）：sms 先取 challenge_id，凭码+设备签名登录；
        成功落 DPAPI 会话。公钥经签名头携带（X-DSJ-Device-Key），body 不带。"""
        self.ensure_pc_auth()
        r = self._request(self.api, "POST", "/login", {
            "challenge_id": challenge_id, "code": code, "client": "pc",
            "device_id": di.device_id(),
            "replace_device": replace_device})
        user = r.get("user") or {}
        uid = user.get("user_id") or user.get("id") or user.get("phone") or "?"
        ttl = r.get("expires_in") or 12 * 3600
        sess = {"user_id": uid, "phone": phone or user.get("phone", ""),
                "bearer": r["access_token"],
                "expires_at": time.strftime(
                    "%Y-%m-%d %H:%M:%S", time.localtime(time.time() + ttl)),
                "expires_at_ts": time.time() + ttl,
                "logged_at": time.strftime("%Y-%m-%d %H:%M:%S")}
        di.save_session(sess)
        return sess

    def me(self):
        bearer, sess = self._bearer()
        if not bearer:
            raise AccountError("invalid_token", "未登录或会话过期", 401)
        return self._request(self.api, "GET", "/me", bearer=bearer)

    def logout(self):
        bearer, _ = self._bearer()
        di.clear_session()
        if bearer:
            try:
                self._request(self.api, "POST", "/logout", bearer=bearer)
            except AccountError:
                pass                          # 服务器拒了也照样本地清干净

    def session_summary(self):
        """UI 状态行：未登录 → {}；已登录 → {user_id/phone/剩余秒}。"""
        sess = di.load_session()
        if not sess.get("bearer"):
            return {}
        exp = sess.get("expires_at_ts", 0)
        out = dict(sess)
        out["expires_in"] = int(exp - time.time())
        if out["expires_in"] <= 0:
            di.clear_session()
            return {}
        return out

    # ------------------------------------------------------------ 生产线（C2S-12~14）

    def production_job(self, upload_id, request_id, params=None):
        """C2S-12 生产申请（幂等键 (user_id, request_id)，端侧只报上传回执
        与可选参数，额度由服务端权益决定）。"""
        bearer, _ = self._bearer()
        if not bearer:
            raise AccountError("invalid_token", "未登录", 401)
        return self._request(self.api, "POST", "/production-jobs", {
            "request_id": request_id, "upload_id": upload_id,
            "params": params or {}}, bearer=bearer)

    def job_status(self, job_id):
        """C2S-13 任务查询（cut_server，签名+账号 Bearer）。"""
        bearer, _ = self._bearer()
        if not bearer:
            raise AccountError("invalid_token", "未登录", 401)
        from urllib.parse import quote
        return self._request(self.prod, "GET",
                             f"/jobs/{quote(job_id, safe='')}", bearer=bearer)

    def open_artifact(self, job_id, file_id):
        """C2S-14 产物读取：返回 (bytes, content_type)。大文件改流式时再
        演进；首版产物（成片/封面）量级直接可载。"""
        bearer, _ = self._bearer()
        if not bearer:
            raise AccountError("invalid_token", "未登录", 401)
        r = self._request(self.prod, "GET",
                          f"/jobs/{job_id}/artifacts/{file_id}", bearer=bearer,
                          timeout=300)
        if "_raw" in r:
            return r["_raw"]
        raise AccountError("bad_response", "产物响应非二进制")
