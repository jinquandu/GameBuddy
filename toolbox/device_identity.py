"""设备身份与 DSJ-PC-1 请求签名（契约 §4A DSJ-PC-1 规范细则，1.4.0）。

安装密钥 Ed25519：首次使用生成一次、此后恒定；Windows 下私钥经 DPAPI
（CryptProtectData，绑定当前用户）落盘 data/.device/key.bin，拷走文件在
别的用户/机器上解不开；非 Windows 平台 0600 明文（仅开发环境）。

device_id = dv- + sha256(原始公钥 32B) hex 前 32 位——公钥摘要，复制一串
码不能登录（REQ-002 C）。签名规范化串与请求头见契约 §4A「DSJ-PC-1 规范
细则」；本模块是该规范的端侧唯一实现，服务端/账号侧按同一规范验签。

依赖 cryptography（ed25519）；DPAPI 走 ctypes 无额外依赖。
"""
import base64
import ctypes
import hashlib
import json
import os
import secrets
import time
from pathlib import Path

from toolbox.config import DATA

DEVICE_DIR = Path(  # 可用环境变量 DSJ_DEVICE_DIR 重定向(多实例/联调;
    os.environ.get("DSJ_DEVICE_DIR") or (DATA / ".device"))
KEY_FILE = DEVICE_DIR / "pc-device-v1.dpapi"
SESSION_FILE = DEVICE_DIR / "session.json"      # DPAPI 包裹的账号会话
REVOKE_FILE = DEVICE_DIR / "session_revoked"    # 服务端吊销留痕(仅码/时间)

SPEC = "DSJ-PC-1"


# ---------------------------------------------------------------- DPAPI（Windows）

class _BLOB(ctypes.Structure):
    _fields_ = [("cbData", ctypes.c_uint), ("pbData", ctypes.c_void_p)]


def _dpapi_fns():
    crypt32 = ctypes.windll.crypt32          # 仅 nt 可用
    for name in ("CryptProtectData", "CryptUnprotectData"):
        fn = getattr(crypt32, name)
        fn.argtypes = [ctypes.POINTER(_BLOB), ctypes.c_wchar_p, ctypes.c_void_p,
                       ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint,
                       ctypes.POINTER(_BLOB)]
        fn.restype = ctypes.c_int
    ctypes.windll.kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    ctypes.windll.kernel32.LocalFree.restype = ctypes.c_void_p
    return crypt32


def _dp_protect(blob: bytes) -> bytes:
    fn = _dpapi_fns()
    buf = ctypes.create_string_buffer(blob)
    bin_ = _BLOB(len(blob), ctypes.cast(buf, ctypes.c_void_p))
    out = _BLOB()
    if not fn.CryptProtectData(ctypes.byref(bin_), None, None, None, None, 0,
                               ctypes.byref(out)):
        raise OSError("CryptProtectData 失败")
    try:
        return ctypes.string_at(out.pbData, out.cbData)
    finally:
        ctypes.windll.kernel32.LocalFree(out.pbData)


def _dp_unprotect(blob: bytes) -> bytes:
    fn = _dpapi_fns()
    buf = ctypes.create_string_buffer(blob)
    bin_ = _BLOB(len(blob), ctypes.cast(buf, ctypes.c_void_p))
    out = _BLOB()
    if not fn.CryptUnprotectData(ctypes.byref(bin_), None, None, None, None, 0,
                                 ctypes.byref(out)):
        raise OSError("CryptUnprotectData 失败（文件不属于当前用户/机器？）")
    try:
        return ctypes.string_at(out.pbData, out.cbData)
    finally:
        ctypes.windll.kernel32.LocalFree(out.pbData)


def _wrap(blob: bytes) -> bytes:
    """落盘前包裹：Windows=DPAPI；其他平台明文（开发环境）。"""
    if os.name == "nt":
        return b"DP01" + _dp_protect(blob)
    return b"DP00" + blob


def _unwrap(blob: bytes) -> bytes:
    if blob[:4] == b"DP01":
        return _dp_unprotect(blob[4:])
    if blob[:4] == b"DP00":
        return blob[4:]
    raise OSError("未知的密钥封装格式")


def _secure_write(path: Path, data: bytes):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    with open(tmp, "wb") as f:
        f.write(data)
        f.flush()
        os.fsync(f.fileno())
    os.chmod(tmp, 0o600)
    tmp.replace(path)


# ---------------------------------------------------------------- 安装密钥

def _load_or_create_key():
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    if KEY_FILE.is_file():
        seed = _unwrap(KEY_FILE.read_bytes())
        return Ed25519PrivateKey.from_private_bytes(seed)
    key = Ed25519PrivateKey.generate()
    seed = key.private_bytes_raw()
    _secure_write(KEY_FILE, _wrap(seed))
    return key


_KEY = None


def device_key():
    """安装密钥（进程内单例；首次调用生成并落盘）。"""
    global _KEY
    if _KEY is None:
        _KEY = _load_or_create_key()
    return _KEY


def public_key_bytes() -> bytes:
    return device_key().public_key().public_bytes_raw()


def public_key_b64() -> str:
    return base64.urlsafe_b64encode(public_key_bytes()).decode().rstrip("=")


def device_id() -> str:
    """sha256(原始公钥) hex 前 32 位（契约 §4A 细则，无前缀）。"""
    return hashlib.sha256(public_key_bytes()).hexdigest()[:32]


def device_code() -> str:
    """展示码（UI 用）：DSJ-XXXXXXXX-XXXXXXXX-XXXXXXXX-XXXXXXXX。"""
    d = device_id()
    return "DSJ-" + "-".join(d[i:i + 8].upper() for i in range(0, 32, 8))


# ---------------------------------------------------------------- 签名器

def _b64url(sig: bytes) -> str:
    return base64.urlsafe_b64encode(sig).decode().rstrip("=")


def canonical_payload(method: str, raw_path_with_query: str,
                      body: bytes, bearer: str, ts, nonce: str) -> bytes:
    """契约 §4A 规范化串（对齐原始规范）：8 行 LF 连接，path 含 query 一体，
    body/bearer 均取 sha256 hex。"""
    body_hex = hashlib.sha256(body or b"").hexdigest()
    bearer_hex = hashlib.sha256((bearer or "").encode("utf-8")).hexdigest()
    return "\n".join([SPEC, method.upper(), raw_path_with_query,
                      body_hex, bearer_hex, device_id(), str(ts),
                      nonce]).encode("utf-8")


def sign_request(method: str, url_path: str, body: bytes = b"",
                 bearer: str = "") -> dict:
    """对一次请求生成 DSJ-PC-1 签名头（五头，公钥随头——服务端免登记验签）。
    url_path 为线上原样 path（含 query 一体）。"""
    ts = int(time.time())
    nonce = secrets.token_hex(16)
    payload = canonical_payload(method, url_path, body or b"",
                                bearer or "", ts, nonce)
    sig = device_key().sign(payload)
    return {"X-DSJ-Device-Id": device_id(),
            "X-DSJ-Device-Key": public_key_b64(),
            "X-DSJ-Device-Time": str(ts),
            "X-DSJ-Device-Nonce": nonce,
            "X-DSJ-Device-Signature": _b64url(sig)}


def verify_request(headers: dict, method: str, raw_path_with_query: str,
                   body: bytes, bearer: str, pubkey: bytes) -> bool:
    """验签（测试/服务端参考实现共用语义；PC 生产路径不本地验自己的签名）。"""
    hl = {k.lower(): v for k, v in headers.items()}
    ts = hl.get("x-dsj-device-time", "")
    nonce = hl.get("x-dsj-device-nonce", "")
    dev = hl.get("x-dsj-device-id", "")
    sig_b64 = hl.get("x-dsj-device-signature", "")
    if not (ts and nonce and dev and sig_b64):
        return False
    try:
        # canonical 的 device 行用请求头里的 dev（验对方/本机任一签名者，
        # 不取本机单例 device_id()——交叉验签实测踩过）
        body_hex = hashlib.sha256(body or b"").hexdigest()
        bearer_hex = hashlib.sha256((bearer or "").encode("utf-8")).hexdigest()
        payload = "\n".join([SPEC, method.upper(), raw_path_with_query,
                             body_hex, bearer_hex, dev, str(ts),
                             nonce]).encode("utf-8")
        sig = base64.urlsafe_b64decode(sig_b64 + "=" * (-len(sig_b64) % 4))
        from cryptography.hazmat.primitives.asymmetric.ed25519 import \
            Ed25519PublicKey
        Ed25519PublicKey.from_public_bytes(pubkey).verify(sig, payload)
        return True
    except Exception:
        return False


# ---------------------------------------------------------------- 账号会话存储

def save_session(sess: dict):
    """会话（含 bearer）DPAPI 包裹落盘；明文不进普通配置/日志。

    写入成功会话 = 重新登录成功，顺手清掉吊销留痕（否则界面会一直提示
    「登录已失效」）。"""
    blob = json.dumps(sess, ensure_ascii=False).encode("utf-8")
    _secure_write(SESSION_FILE, _wrap(blob))
    clear_revoked()


def mark_revoked(code="", message=""):
    """记录「服务端已吊销本机会话」。只存错误码/时间，不存令牌。

    为什么持久化：prerequisites 发现 401 时会清掉本地会话，若不留痕，
    下一次刷新就只能显示「未登录」，用户看不出是「被吊销需要重新登录」
    还是「从没登录过」（2026-10-22 本机验收发现）。"""
    try:
        DEVICE_DIR.mkdir(parents=True, exist_ok=True)
        f = REVOKE_FILE
        f.write_text(json.dumps(
            {"code": str(code or "unauthorized")[:64],
             "at": time.strftime("%Y-%m-%d %H:%M:%S")},
            ensure_ascii=False), encoding="utf-8")
    except OSError:
        pass


def revoked():
    """-> {"code","at"} 或 {}；无留痕/损坏返回 {}。"""
    try:
        d = json.loads(REVOKE_FILE.read_text(encoding="utf-8"))
        return d if isinstance(d, dict) and d.get("code") else {}
    except (OSError, ValueError):
        return {}


def clear_revoked():
    try:
        REVOKE_FILE.unlink(missing_ok=True)
    except OSError:
        pass


def load_session() -> dict:
    if not SESSION_FILE.is_file():
        return {}
    try:
        return json.loads(_unwrap(SESSION_FILE.read_bytes()).decode("utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def clear_session():
    SESSION_FILE.unlink(missing_ok=True)
