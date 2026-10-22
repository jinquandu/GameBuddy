"""新手教程(REQ-20261022-002):安装登录后 ~10 分钟全流程体验。

真实机制可视化 + 服务端短路:扫描/队列/徽标/作品页全走真代码,仅「识别+
云端混剪」被服务端 C2S-16 识别为教程后跳过(即时返回 done + demo 作品页),
客户端自行演出 60-90s「剪辑中」节奏。

守卫(REQ 铁律):
- 教程会话名 TUTORIAL_SESSION 固定,不写 last_production_ts(不占客户端
  24h 滚动窗口——否则用户首次真实生产会被教程锁一天);
- 不触发真实上传/生产;服务端 C2S-16 零生产副作用(不建候选组/不入队列);
- 沙盒(素材+队列记录)在清理时整体移除,可重放;
- 素材下载 Range 断点续传,SHA-256 校验。

C2S-16(契约 1.4.1):POST /api/production/v1/tutorial——仅登录(豁免
权益/绑定),服务端零生产副作用,每账号每日 ≤3 次(超限 429)。
"""
import hashlib
import json
import time
import urllib.request
from pathlib import Path

from toolbox.config import DATA, load_config
from toolbox.folder_watch import TUTORIAL_SESSION
from toolbox.logs import get as _getlog

LOG = _getlog("tutorial")

DEMO_URL = "http://8.153.157.239/eval/clients/dashijie-tutorial-demo-input.mp4"
DEMO_SHA256 = "c10df0870c7e3489392d4a2c0ab9b3a55f6debb028be900ceab26177982e7e3e"
TUTORIAL_DIR = DATA / "tutorial"


def sandbox_dir(watch_dir=None):
    """教程沙盒目录 = 监控目录下的教程会话(走真实扫描)。watch_dir 语义
    同 plant_sandbox。"""
    wdir = watch_dir if watch_dir is not None else         (load_config().get("folder_watch") or {}).get("watch_dir")
    return (Path(wdir) / TUTORIAL_SESSION) if wdir else None


def _done_flag():
    return TUTORIAL_DIR / "done.json"      # 动态取(测试可重定向 TUTORIAL_DIR)


def is_done():
    try:
        return bool(json.loads(_done_flag().read_text(
            encoding="utf-8")).get("done"))
    except (OSError, ValueError):
        return False


def mark_done():
    TUTORIAL_DIR.mkdir(parents=True, exist_ok=True)
    _done_flag().write_text(json.dumps(
        {"done": True, "at": time.strftime("%Y-%m-%d %H:%M:%S")},
        ensure_ascii=False), encoding="utf-8")


def reset(watch_dir=None):
    """重放:清沙盒(监控目录下的教程会话)+素材+完成标记。watch_dir 语义
    同 plant_sandbox。"""
    import shutil
    sd = sandbox_dir(watch_dir)
    if sd:
        shutil.rmtree(sd, ignore_errors=True)
    shutil.rmtree(TUTORIAL_DIR, ignore_errors=True)


def material_path():
    return TUTORIAL_DIR / "demo-input.mp4"


def material_ready():
    p = material_path()
    if not p.is_file():
        return False
    return hashlib.sha256(p.read_bytes()).hexdigest() == DEMO_SHA256


def download_material(progress=None, chunk=1 << 20):
    """下载 demo 素材(Range 断点续传+SHA-256 校验)。progress(frac 0..1)。
    返回 True=就绪。"""
    TUTORIAL_DIR.mkdir(parents=True, exist_ok=True)
    p = material_path()
    if material_ready():
        return True
    tmp = p.with_suffix(".part")
    done = tmp.stat().st_size if tmp.exists() else 0
    req = urllib.request.Request(DEMO_URL)
    if done:
        req.add_header("Range", f"bytes={done}-")
    with urllib.request.urlopen(req, timeout=60) as r:
        total = done + int(r.headers.get("Content-Length") or 0) or None
        with tmp.open("ab") as f:
            while True:
                blob = r.read(chunk)
                if not blob:
                    break
                f.write(blob)
                done += len(blob)
                if progress and total:
                    progress(min(1.0, done / total))
    if hashlib.sha256(p.read_bytes() if p.exists() else b"").hexdigest() \
            != DEMO_SHA256 and p.exists():
        p.unlink()                              # 旧完整文件与素材更新了
    if hashlib.sha256(tmp.read_bytes()).hexdigest() != DEMO_SHA256:
        raise RuntimeError("教程素材校验失败,请重试下载")
    tmp.replace(p)
    if progress:
        progress(1.0)
    return True


def plant_sandbox(watch_dir=None):
    """把素材落成教程会话:复制到监控目录下的「新手体验-demo/」,供真实
    扫描入队(用户看到真机制)。watch_dir 显式传入(含空串=无目录)优先;
    None 时读 config。均无目录返回 None。返回落位目录。"""
    src = material_path()
    if not material_ready():
        return None
    wdir = watch_dir if watch_dir is not None else \
        (load_config().get("folder_watch") or {}).get("watch_dir")
    if not wdir:
        return None
    dst = Path(wdir) / TUTORIAL_SESSION
    dst.mkdir(parents=True, exist_ok=True)
    target = dst / "新手体验-demo.mp4"
    if not target.exists() or target.stat().st_size != src.stat().st_size:
        import shutil
        shutil.copy(src, target)
    return dst


def call_tutorial_notify(api_production_url=None):
    """C2S-17(契约 1.4.2,REQ-20261022-003):教程完成微信推送触发。
    返回 {"notified": true|false}——false=未绑定微信/通知服务未配置或
    不可用(客户端静默,结束卡兜底引导);仅登录,共用教程每日配额。"""
    from toolbox.account import require_login
    from toolbox import device_identity as di
    from toolbox import uploader as up
    cfg = (load_config().get("server") or {})
    base = api_production_url or cfg.get("upload_url") \
        or "https://95188.pw/api/production/v1"
    s = require_login()
    b = s["bearer"]
    client = up.ProductionUploader(
        base, token=b,
        signer=lambda m, w, d: di.sign_request(m, w, d, b))
    return client._request("POST", "/tutorial/notify", body={})


def call_tutorial_api(api_production_url=None):
    """C2S-16(契约 1.4.1):登录签名请求教程识别豁免。返回响应 dict;
    429=今日超限;服务端未上线(404)= TutorialNotAvailable。"""
    from toolbox.account import require_login, AccountError
    from toolbox import device_identity as di
    from toolbox import uploader as up
    cfg = (load_config().get("server") or {})
    base = api_production_url or cfg.get("upload_url") \
        or "https://95188.pw/api/production/v1"
    s = require_login()                         # 教程仅要求登录
    b = s["bearer"]
    client = up.ProductionUploader(
        base, token=b,
        signer=lambda m, w, d: di.sign_request(m, w, d, b))
    try:
        return client._request("POST", "/tutorial", body={})
    except up.UploadError as e:
        if e.status == 404:
            raise TutorialNotAvailable() from None
        raise AccountError(e.code, str(e), e.status) from None


class TutorialNotAvailable(RuntimeError):
    """服务端未部署 C2S-16(旧版后端)。教程降级:仍可看素材与 UI 引导,
    但无作品页环节。"""
