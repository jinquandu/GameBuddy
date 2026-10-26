"""更新清单签名(REQ-20261026-002):Ed25519 验签,公钥编译进客户端。

为什么签名:更新清单走公网静态位(免登录 GET),光靠 sha256 防不了
「清单+包一起被篡改」。签名保证:服务器/通道被入侵的攻击者**推不了
恶意更新**——最坏把清单改坏=客户端验签失败整体作废、沿用当前版本
(等于停更),不能投毒。

密钥分工:
- 公钥(下方 UPDATE_PUBKEY,Raw 32 字节 hex)随客户端分发;
- 私钥 PEM 由用户**离线保管**(生成于 2026-10-26,已当面交付,
  不进任何仓库/服务器/打包机);发布用 deploy/sign_update.py;
- 轮换:清单可带 next_pubkey(同样纳入签名),验证通过后新公钥生效。

签名对象:canonical(payload) —— 对「可执行内容」的确定性 JSON 串
(sort_keys + 紧凑分隔符 + ensure_ascii=False)。清单里的未知字段不进
payload(改了不影响验签,也不会被执行)。
"""
import json

from toolbox.logs import get as _getlog

LOG = _getlog("pet")

# 生产公钥(Ed25519 Raw,hex)。私钥离线;轮换走 next_pubkey。
UPDATE_PUBKEY = ("3fab2c444989c1d3baecc00e7e008a5e65f6c0f14600c4102872799"
                 "a54f48a53")

# 纳入签名的 latest 字段(白名单:多余字段忽略,缺失按空值签名)
LATEST_FIELDS = ("version", "build", "url", "sha256", "size", "notes",
                 "min_required", "released_at")


def canonical(channel_disabled, latest, next_pubkey=""):
    """确定性签名串:与客户端/发布工具共用,字段排序+紧凑 JSON。"""
    payload = {
        "channel_disabled": bool(channel_disabled),
        "latest": {k: latest.get(k, "") for k in LATEST_FIELDS},
        "next_pubkey": str(next_pubkey or ""),
    }
    return json.dumps(payload, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"))


def verify(channel_disabled, latest, next_pubkey, sig_hex, pubkey_hex=None):
    """验签(True/False)。pubkey_hex 缺省用内置公钥;测试可注入。"""
    try:
        from cryptography.hazmat.primitives.asymmetric.ed25519 import (
            Ed25519PublicKey)
        from cryptography.hazmat.primitives.serialization import (
            Encoding, PublicFormat)
        pub = Ed25519PublicKey.from_public_bytes(
            bytes.fromhex(pubkey_hex or UPDATE_PUBKEY))
        # 触发一次序列化确保密钥合法;直接验签即可
        _ = pub.public_bytes(Encoding.Raw, PublicFormat.Raw)
        data = canonical(channel_disabled, latest, next_pubkey).encode("utf-8")
        pub.verify(bytes.fromhex(str(sig_hex or "").strip()), data)
        return True
    except Exception as e:                    # noqa: BLE001 验签失败一律 False
        LOG.info("更新清单验签失败:%s", e)
        return False


def sign(channel_disabled, latest, private_pem_bytes, next_pubkey=""):
    """发布工具用:私钥签名 -> sig hex。客户端不走此路径。"""
    from cryptography.hazmat.primitives.asymmetric.ed25519 import (
        Ed25519PrivateKey)
    from cryptography.hazmat.primitives.serialization import load_pem_private_key
    key = load_pem_private_key(private_pem_bytes, password=None)
    if not isinstance(key, Ed25519PrivateKey):
        raise SystemExit("私钥不是 Ed25519(请用生成工具产出的 PEM)")
    data = canonical(channel_disabled, latest, next_pubkey).encode("utf-8")
    return key.sign(data).hex()
