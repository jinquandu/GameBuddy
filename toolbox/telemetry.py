# -*- coding: utf-8 -*-
"""客户端统计遥测参数(REQ-20261028-002 方案 A)。

背景:客户端原先从不把程序版本发出去,服务端无从知道"多少人在用、
各是什么 build"。最轻的补法不是新增上报接口,而是在**已有的两个定时
轮询**(规则 6h / 更新 6h)URL 上追加查询参数——nginx 静态 alias 直出
时忽略 query,客户端拿到的响应完全不变,服务端 access_log 多记一行。

原则:
- 只带 build/版本/平台三个**非身份**字段;绝不带手机号、device_id、
  token(统计日志不得变成身份库);
- 服务端可忽略未知 query(旧服务端照常返回文件);
- 任何异常都不影响轮询——拼参数失败就退回不带参数的 URL。
"""
import sys

from toolbox.version import BUILD, VERSION


def query():
    """-> "build=20261028-r16&v=0.1&os=win"(纯 ASCII,无需转义)。"""
    osname = "win" if sys.platform == "win32" else (
        "mac" if sys.platform == "darwin" else sys.platform)
    return f"build={BUILD}&v={VERSION}&os={osname}"


def with_query(url):
    """给 URL 追加统计参数;已有 query 用 & 连接;空 URL 原样返回。"""
    u = str(url or "").strip()
    if not u:
        return u
    sep = "&" if "?" in u else "?"
    return f"{u}{sep}{query()}"

# ---------------------------------------------------------------- 安装信标
#
# 方案 B 修订(2026-10-28 用户质疑"为什么提给账号侧"):不依赖账号服务。
# 客户端首启生成一个**随机 install_id**(与设备密钥无关、不含任何身份信息),
# 每 24 小时向**服务端静态分发位**发一次 GET 信标:
#   /eval/clients/beat.json?install=<uuid>&build=...&v=...&os=...
# beat.json 是发布位上的静态占位文件(200),nginx 在该 location 记日志,
# 统计脚本按 install_id 去重 → **精确的唯一安装数**(不像 IP 会被 NAT 合并)。
# 全程 fire-and-forget:失败静默、不重试、不弹窗,绝不影响任何功能。

BEAT_PATH = "/eval/clients/beat.json"
BEAT_INTERVAL_S = 24 * 3600
IP_FALLBACK = "http://8.153.157.239"
_install_cache = {"id": None}


def install_id():
    """本机安装标识:首启生成随机 UUID 并持久化(非敏感,明文存储)。
    与设备鉴权密钥无关——重装/换机会变,正好用来数"安装"而非"设备"。"""
    if _install_cache["id"]:
        return _install_cache["id"]
    from toolbox.config import DATA
    p = DATA / ".device" / "install_id"
    try:
        v = p.read_text(encoding="utf-8").strip()
        if len(v) >= 16:
            _install_cache["id"] = v
            return v
    except OSError:
        pass
    import uuid
    v = uuid.uuid4().hex
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(v, encoding="utf-8")
    except OSError:
        pass                                  # 落盘失败仍返回本次值
    _install_cache["id"] = v
    return v


def beat_urls():
    """信标候选地址(与规则/清单同源策略:显式配置→origin→IP 兜底)。"""
    from toolbox.config import load_config
    s = load_config().get("server") or {}
    out = []
    u = str(s.get("beat_url") or "").strip()
    if u:
        out.append(u)
    origin = str(s.get("upload_url") or "").strip()
    if "/api/" in origin:
        origin = origin.rsplit("/api/", 1)[0]
    if origin:
        out.append(origin + BEAT_PATH)
    fb = IP_FALLBACK + BEAT_PATH
    if fb not in out:
        out.append(fb)
    return [with_query(u) for u in out]


def send_beat(timeout=8, _open=None):
    """发一次安装信标(任一候选成功即返回 True)。任何异常都吞掉。
    _open(url, timeout) 可注入(测试)。"""
    import urllib.request
    opener = _open or (lambda u, timeout: urllib.request.urlopen(u, timeout=timeout))
    iid = install_id()
    for base in beat_urls():                  # beat_urls 已带 build/v/os query
        url = f"{base}&install={iid}"
        try:
            with opener(url, timeout) as r:
                r.read(1)
            return True
        except Exception:                     # noqa: BLE001 统计失败不影响功能
            continue
    return False

