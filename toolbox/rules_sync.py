"""客户端规则服务端下发与对齐(REQ-20261026-001 规则2)。

为什么:阈值/扫描节拍/稳定判稳这类规则会随运营调整(如 2GB→1GB、
扫描 2h→1h)。每次改规则重发安装包成本高;由客户端定时从服务端拉一份
规则 JSON,与内置默认对齐,即可远程调整。

通道选择:复用既有公网静态分发位(/eval/clients/,免登录直链,与安装包
同源)放 client-rules.json——**不是新增 API**,不进 SERVER-INTERFACE
契约;服务端只托管一个静态文件,改规则=改这个文件。

安全边界(评审口径):
- 只下发**非安全**的行为参数(白名单+夹紧范围),登录/绑定/权益等
  安全门不在此列,永远以服务端受理层为准;
- 客户端对每个值做类型与范围夹紧,越界/未知键一律忽略——服务端文件
  被篡改最坏效果=参数在合理区间内变化,不可能放开安全门;
- 拉取失败沿用上次缓存,再失败沿用内置默认;绝不阻塞任何流程。

生效方式:applied_overrides() 被 folder_watch._cfg() 合并——
内置默认 < config.yaml < 服务端规则(服务端最高,可被 config 的
rules_url 关闭/改源)。改完下一拍(扫描/生产 10 分钟粒度)自然生效。
"""
import json
import time
import urllib.request
from pathlib import Path

from toolbox.config import DATA, load_config
from toolbox.logs import get as _getlog

LOG = _getlog("pet")

CACHE = DATA / "client_rules.json"
DEFAULT_URL_PATH = "/eval/clients/client-rules.json"
POLL_S = 6 * 3600          # 定时对齐周期(首启 30s 后先拉一次)

# 白名单与夹紧范围:键名 -> (类型, 下限, 上限)
SPEC = {
    "threshold_gb": (float, 0.05, 100.0),        # 生产阈值(GB)
    "scan_interval_min": (int, 10, 1440),        # 扫描节拍(分钟)
    "daily_hour": (int, 0, 23),                  # 生产时刻(时,兼容保留)
    "stable_min": (int, 1, 240),                 # 判稳分钟数
    "production_window_s": (int, 3600, 7 * 86400),  # 滚动窗口(兼容保留;
                                                  # 2026-10-28 起不再使用)
    "attempts_max": (int, 1, 10),                # 任务重试上限
    "lease_min": (int, 5, 1440),                 # 崩溃租约回收(分钟)
    "work_start_hour": (int, 0, 23),             # 生产工作时段起(时,
                                                  # 2026-10-28 新规则)
    "work_end_hour": (int, 0, 23),               # 生产工作时段止(时,不含)
    "cpu_yield_percent": (float, 50.0, 100.0),   # CPU 高于该值生产退让
}


IP_FALLBACK = "http://8.153.157.239"     # 公网分发位(安装包同源;95188.pw
                                           # 的 /eval/clients 被作品页收窄排除)


def rules_urls():
    """候选地址(按序尝试):server.rules_url 显式配置 → upload_url 的 origin
    推导 → 公网 IP 分发位兜底。95188.pw 对 /eval/clients 返回 404 是
    REQ-20261022-001 收窄的既有行为,所以 origin 推导常失败——IP 兜底保证
    默认配置开箱即用。全空返回 [](=不拉取)。"""
    s = load_config().get("server") or {}
    out = []
    u = str(s.get("rules_url") or "").strip()
    if u:
        out.append(u)
    origin = str(s.get("upload_url") or "").strip()
    if "/api/" in origin:
        origin = origin.rsplit("/api/", 1)[0]
    if origin:
        out.append(origin + DEFAULT_URL_PATH)
    if IP_FALLBACK + DEFAULT_URL_PATH not in out:
        out.append(IP_FALLBACK + DEFAULT_URL_PATH)
    return out


def _clamp(key, value):
    typ, lo, hi = SPEC[key]
    try:
        x = typ(value)
    except (TypeError, ValueError):
        return None
    return max(lo, min(hi, x))


def parse(raw):
    """服务端 JSON -> 可用覆盖集。兼容两种形态:整份即 folder_watch 键值,
    或 {"folder_watch": {...}}(留出以后按组扩展)。未知键忽略,越界夹紧。"""
    if not isinstance(raw, dict):
        return {}
    body = raw.get("folder_watch")
    body = body if isinstance(body, dict) else raw
    out = {}
    for key in SPEC:
        if key in body:
            v = _clamp(key, body[key])
            if v is not None:
                out[key] = v
    return out


def save(raw):
    """校验+夹紧后落盘缓存(原子写),返回本次应用的覆盖集。"""
    overrides = parse(raw)
    payload = {
        "rules_version": str(raw.get("rules_version", ""))[:64],
        "applied_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "overrides": overrides,
    }
    CACHE.parent.mkdir(parents=True, exist_ok=True)
    tmp = CACHE.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=1),
                   encoding="utf-8")
    tmp.replace(CACHE)
    return overrides


def applied_overrides():
    """当前生效的服务端覆盖(读缓存);无缓存/损坏返回 {}(=用内置默认)。"""
    try:
        d = json.loads(CACHE.read_text(encoding="utf-8"))
        ov = d.get("overrides")
        return {k: v for k, v in (ov or {}).items() if k in SPEC}
    except (OSError, ValueError):
        return {}


def applied_meta():
    """-> {"rules_version","applied_at",...}(诊断显示用);无缓存返回 {}。"""
    try:
        d = json.loads(CACHE.read_text(encoding="utf-8"))
        return {"rules_version": d.get("rules_version", ""),
                "applied_at": d.get("applied_at", "")}
    except (OSError, ValueError):
        return {}


def clear():
    """清空缓存(测试/停用)。"""
    try:
        CACHE.unlink(missing_ok=True)
    except OSError:
        pass


def fetch(url, timeout=10):
    if not url:
        return None
    with urllib.request.urlopen(url, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def poll_once(url=None):
    """拉一次并应用(候选地址按序尝试,全败静默沿用缓存/内置)。
    返回本次应用的覆盖集,失败返回 None。"""
    urls = [url] if url else rules_urls()
    last_err = None
    for u in urls:
        try:
            raw = fetch(u)
            if not isinstance(raw, dict):
                continue
            overrides = save(raw)
            LOG.info("规则已对齐服务端 v%s（%s）：%s",
                     raw.get("rules_version", ""), u,
                     overrides or "(空=用内置默认)")
            return overrides
        except Exception as e:                 # noqa: BLE001 单源失败换下一个
            last_err = e
    if last_err is not None:
        LOG.info("规则拉取跳过(沿用缓存/内置)：%s", last_err)
    return None
