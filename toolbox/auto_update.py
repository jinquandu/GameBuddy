"""客户端自动更新(REQ-20261026-002):发现→验签→下载→就绪;应用(换包)
由用户确认重启后经 update-apply.cmd 完成。

职责边界:
- 本模块只做「把校验过的新包铺到 data/updates/<build>/staged 并生成
  应用脚本」;**绝不杀进程、绝不自动重启**——重启入口在 pet,由用户点击;
- 清单必须通过 Ed25519 验签(update_signing);验签失败=本次作废,
  沿用当前版本并留痕,不存在无签名降级;
- 下载为 Range 断点续传;sha256+size 双校验;数据盘剩余不足或剪辑
  进行中(调用方判断)则让路;
- 应用脚本(GBK 编码 cmd,兼容中文安装路径;注释 ASCII)等旧进程退出后
  robocopy /MIR 换包,**排除 data\\ 与 config.yaml**(账号/设备/队列/
  监控目录不能被默认配置覆盖;排除用完整路径,只作用于应用根,不误伤
  _internal 内同名文件);失败保留旧目录可启动,staged 留待人工重试。

磁盘口径:zip(~390MB)+解包(~830MB)+余量 → 下载前要求剩余 > 3×包大小。
"""
import hashlib
import json
import os
import re
import shutil
import subprocess
import time
import urllib.request
from pathlib import Path

from toolbox import update_signing
from toolbox.config import DATA, ROOT, load_config
from toolbox.logs import get as _getlog
from toolbox.version import BUILD, VERSION

LOG = _getlog("pet")

UPDATE_DIR = DATA / "updates"
IP_FALLBACK = "http://8.153.157.239"
MANIFEST_PATH = "/eval/clients/client-update.json"
SHA_RE = re.compile(r"^[0-9a-fA-F]{64}$")
# build 形如 20261022-r7f -> (日期, 序号, 后缀)
BUILD_RE = re.compile(r"^(\d{8})-r(\d+)([a-z]*)$")
FREE_FACTOR = 3          # 下载前要求剩余 > 3×包大小


class UpdateError(RuntimeError):
    pass


# ---------------------------------------------------------------- 版本比较

def version_tuple(v):
    try:
        return tuple(int(p) for p in str(v).split("."))
    except ValueError:
        return (0,)


def build_tuple(b):
    m = BUILD_RE.match(str(b))
    if not m:
        return (0, 0, "")
    return (int(m.group(1)), int(m.group(2)), m.group(3))


def newer_than(ver_new, build_new, ver_cur=VERSION, build_cur=BUILD):
    """新清单是否比当前版本新:先比语义版本,同版本比构建号。"""
    vn, vc = version_tuple(ver_new), version_tuple(ver_cur)
    if vn != vc:
        return vn > vc
    return build_tuple(build_new) > build_tuple(build_cur)


def required(ver_cur=VERSION, min_required=""):
    """当前版本低于 min_required → 强提示(仍不强制重启)。"""
    mr = str(min_required or "").strip()
    if not mr:
        return False
    return version_tuple(ver_cur) < version_tuple(mr)


# ---------------------------------------------------------------- 清单

def manifest_urls():
    """候选地址:config server.update_url 显式 → upload_url origin → IP。"""
    s = load_config().get("server") or {}
    out = []
    u = str(s.get("update_url") or "").strip()
    if u:
        out.append(u)
    origin = str(s.get("upload_url") or "").strip()
    if "/api/" in origin:
        origin = origin.rsplit("/api/", 1)[0]
    if origin:
        out.append(origin + MANIFEST_PATH)
    fb = IP_FALLBACK + MANIFEST_PATH
    if fb not in out:
        out.append(fb)
    return out


def _abs(url, base_urls):
    """清单里的 url 可能是相对路径(/eval/...),按可用 origin 拼绝对。"""
    if url.startswith("http://") or url.startswith("https://"):
        return url
    for b in base_urls:
        origin = b.rsplit(MANIFEST_PATH, 1)[0] if MANIFEST_PATH in b else b
        if origin:
            return origin.rstrip("/") + url
    return IP_FALLBACK + url


def parse_manifest(raw, pubkey_hex=None):
    """原始 JSON -> {latest, disabled, required, pkg_url, sig_ok}。
    验签失败抛 UpdateError(调用方捕获后沿用当前版本)。
    pubkey_hex 缺省用内置公钥(测试可注入)。"""
    if not isinstance(raw, dict):
        raise UpdateError("清单不是 JSON 对象")
    sig = str(raw.get("sig") or "").strip()
    if not sig:
        raise UpdateError("清单无签名")
    disabled = bool(raw.get("channel_disabled"))
    latest = raw.get("latest")
    if not isinstance(latest, dict):
        raise UpdateError("清单缺 latest")
    latest = {k: latest.get(k, "") for k in update_signing.LATEST_FIELDS}
    if not SHA_RE.match(str(latest.get("sha256") or "")):
        raise UpdateError("sha256 格式不对")
    try:
        latest["size"] = int(latest.get("size") or 0)
    except (TypeError, ValueError):
        raise UpdateError("size 格式不对") from None
    if latest["size"] <= 0:
        raise UpdateError("size 非正")
    next_pk = str(raw.get("next_pubkey") or "").strip()
    if not update_signing.verify(disabled, latest, next_pk, sig,
                                 pubkey_hex=pubkey_hex):
        raise UpdateError("验签失败(清单作废)")
    return {
        "latest": latest,
        "disabled": disabled,
        "required": required(min_required=latest.get("min_required")),
        "newer": newer_than(latest.get("version"), latest.get("build")),
        "pkg_url": _abs(str(latest.get("url") or ""), manifest_urls()),
        "pkg_urls": _url_candidates(str(latest.get("url") or "")),
        "size": int(latest["size"]),
        "sig_ok": True,
        "next_pubkey": next_pk,
    }


def _url_candidates(rel_url):
    """包下载候选(相对 url 拼每个可用 origin;绝对 url 原样;含 IP 兜底)。
    95188 origin 对 /eval/clients 是 404(作品页收窄),下载按序尝试。"""
    if rel_url.startswith("http://") or rel_url.startswith("https://"):
        return [rel_url]
    out = []
    for b in manifest_urls():
        origin = b.rsplit(MANIFEST_PATH, 1)[0] if MANIFEST_PATH in b else b
        u = (origin.rstrip("/") + rel_url) if origin else (IP_FALLBACK + rel_url)
        if u not in out:
            out.append(u)
    return out


def download_any(urls, sha256, size, dest_zip, **kw):
    """候选地址按序下载;单源失败(404/断连)换下一个,全败抛最后一个错。"""
    last = None
    for u in urls:
        try:
            return download(u, sha256, size, dest_zip, **kw)
        except UpdateError as e:
            last = e
    if last is not None:
        raise last
    raise UpdateError("无下载地址")


def fetch_manifest(urls=None, timeout=10):
    """候选地址按序拉清单;全败返回 None。"""
    last = None
    for u in (urls or manifest_urls()):
        try:
            with urllib.request.urlopen(u, timeout=timeout) as r:
                return json.loads(r.read().decode("utf-8"))
        except Exception as e:                 # noqa: BLE001 换下一个候选
            last = e
    if last is not None:
        LOG.info("更新清单拉取失败:%s", last)
    return None


def check():
    """一步式检查 -> parse_manifest 结果或 {"unavailable": True}。"""
    raw = fetch_manifest()
    if raw is None:
        return {"unavailable": True}
    try:
        return parse_manifest(raw)
    except UpdateError as e:
        LOG.warning("更新清单作废:%s", e)
        return {"invalid": True, "reason": str(e)}


# ---------------------------------------------------------------- 下载

class _HttpRange:
    """Range 响应句柄(流式读,不整包进内存)。"""

    def __init__(self, resp):
        self._r = resp

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        try:
            self._r.close()
        except Exception:                      # noqa: BLE001
            pass
        return False

    def read(self, n=-1):
        return self._r.read(n)

    @property
    def total(self):
        try:
            return int(self._r.headers.get("Content-Length") or 0)
        except (TypeError, ValueError):
            return 0


def _open_range(url, start, timeout=60):
    req = urllib.request.Request(url)
    if start:
        req.add_header("Range", f"bytes={start}-")
    return _HttpRange(urllib.request.urlopen(req, timeout=timeout))


def free_bytes(path=None):
    p = Path(path or UPDATE_DIR)
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        return shutil.disk_usage(str(p)).free
    except OSError:
        return 0


def _file_sha256(path, chunk=1 << 20):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for b in iter(lambda: f.read(chunk), b""):
            h.update(b)
    return h.hexdigest()


def download(pkg_url, sha256, size, dest_zip, chunk=1 << 20,
             opener=None, on_progress=None):
    """断点续传下载+sha256/size 双校验(流式,不整包进内存)。返回最终
    zip 路径;失败删 .part 抛 UpdateError(调用方下一拍重试)。
    opener(url, start) 可注入(测试),缺省走 HTTP Range。"""
    opener = opener or _open_range
    dest = Path(dest_zip)
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.is_file():
        if _file_sha256(dest) == sha256 and dest.stat().st_size == size:
            return dest                     # 之前已下完
        dest.unlink(missing_ok=True)
    part = dest.with_suffix(".zip.part")
    done = part.stat().st_size if part.exists() else 0
    if done > size:
        part.unlink(missing_ok=True)        # 旧 .part 与新包不匹配
        done = 0
    h = hashlib.sha256()
    if done:
        h.update(part.read_bytes())         # 续传前先累计已下载部分的摘要
    with opener(pkg_url, done) as r:
        while True:
            blob = r.read(chunk)
            if not blob:
                break
            with part.open("ab") as f:
                f.write(blob)
            h.update(blob)
            done += len(blob)
            if on_progress:
                on_progress(min(1.0, done / size))
    if done != size:
        part.unlink(missing_ok=True)
        raise UpdateError("下载中断(大小不符)")
    if h.hexdigest() != sha256:
        part.unlink(missing_ok=True)
        raise UpdateError("sha256 校验失败")
    part.replace(dest)
    return dest


# ---------------------------------------------------------------- 就绪与清理

def stage_dir(build):
    return UPDATE_DIR / str(build) / "staged"


def zip_path(build):
    return UPDATE_DIR / str(build) / "pkg.zip"


def stage(build, zp):
    """解包已校验的 zip 到 staged/(顶层 GameBuddy/ 保留),并做存在性抽验。"""
    import zipfile
    sd = stage_dir(build)
    if (sd / "GameBuddy" / "GameBuddy.exe").is_file():
        return sd                              # 已就绪(幂等)
    if sd.exists():
        shutil.rmtree(sd, ignore_errors=True)
    sd.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(zp) as z:
        names = z.namelist()
        if sum(1 for n in names if "\\" in n or n.startswith("/")):
            shutil.rmtree(sd, ignore_errors=True)
            raise UpdateError("包内路径不规范")
        z.extractall(sd)
    exe = sd / "GameBuddy" / "GameBuddy.exe"
    if not exe.is_file() or exe.stat().st_size < 1_000_000:
        shutil.rmtree(sd, ignore_errors=True)
        raise UpdateError("解包抽验失败(缺 exe)")
    return sd


def cleanup_old(keep_build):
    """只保留最新 build 的下载物,清掉历史(省磁盘)。"""
    keep = str(keep_build)
    try:
        for d in UPDATE_DIR.iterdir():
            if d.is_dir() and d.name != keep:
                shutil.rmtree(d, ignore_errors=True)
    except OSError:
        pass


def ready_info():
    """落盘的就绪状态(重启后仍可「重启并更新」)。"""
    try:
        d = json.loads((UPDATE_DIR / "state.json").read_text(encoding="utf-8"))
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def save_ready(info):
    UPDATE_DIR.mkdir(parents=True, exist_ok=True)
    tmp = UPDATE_DIR / "state.tmp"
    tmp.write_text(json.dumps(info, ensure_ascii=False, indent=1),
                   encoding="utf-8")
    tmp.replace(UPDATE_DIR / "state.json")


# ---------------------------------------------------------------- 应用脚本

APPLY_NAME = "update-apply.cmd"


def build_apply_cmd(app_dir, staged, old_pid, log_path):
    """生成换包脚本(GBK 编码以兼容中文安装路径;内容注释 ASCII)。

    脚本流程:等旧 PID 退出(至多 120s)→ robocopy /MIR 换包(排除
    data\\ 与 config.yaml,用完整路径限定在应用根)→ 启动新 exe →
    清 staged 与自身。robocopy 返回码 ≥8 视为失败:写日志、保留旧目录。
    """
    app = Path(app_dir).resolve()
    src = Path(staged).resolve() / "GameBuddy"
    data_dir = app / "data"
    cfg = app / "config.yaml"
    exe_new = src / "GameBuddy.exe"
    for p in (app, src, exe_new):
        if not p.exists():
            raise UpdateError(f"应用脚本前置缺失:{p}")
    lines = [
        "@echo off",
        "rem GameBuddy self-update apply (REQ-20261026-002). ASCII only.",
        "rem Waits for the old process to exit, mirrors the staged build",
        "rem over the app dir (data\\ and config.yaml excluded), restarts.",
        "setlocal",
        f"set OLD_PID={int(old_pid)}",
        f"set APP={app}",
        f"set SRC={src}",
        f"set LOG={Path(log_path).resolve()}",
        "set TRIES=0",
        ":wait",
        "tasklist /FI \"PID eq %OLD_PID%\" 2>nul | find \"%OLD_PID%\" >nul",
        "if errorlevel 1 goto doapply",
        "set /a TRIES+=1",
        "if %TRIES% GEQ 120 goto fail_timeout",
        "timeout /t 1 /nobreak >nul",
        "goto wait",
        ":doapply",
        f"robocopy \"%SRC%\" \"%APP%\" /MIR /R:2 /W:2 /NFL /NDL "
        f"/XD \"{data_dir}\" /XF \"{cfg}\" \"{app}\\{APPLY_NAME}\" "
        f">>\"%LOG%\" 2>&1",
        "if errorlevel 8 goto fail_copy",
        f"start \"\" \"%APP%\\GameBuddy.exe\"",
        f"rd /s /q \"{src.parent}\" >nul 2>&1",
        "del \"%~f0\" >nul 2>&1",
        "exit /b 0",
        ":fail_timeout",
        "echo old process did not exit >>\"%LOG%\"",
        "exit /b 1",
        ":fail_copy",
        "echo robocopy failed, old install kept >>\"%LOG%\"",
        "exit /b 1",
    ]
    cmd_path = app / APPLY_NAME
    body = "\r\n".join(lines) + "\r\n"
    try:
        cmd_path.write_bytes(body.encode("gbk"))
    except UnicodeEncodeError as e:
        raise UpdateError(f"安装路径含 GBK 外字符,暂不支持自动更新:{e}") from None
    return cmd_path


def launch_apply(cmd_path):
    """拉起应用脚本(分离+无窗口);真正的换包在旧进程退出后进行。"""
    subprocess.Popen(["cmd", "/c", "start", "/min", "", str(cmd_path)],
                     creationflags=subprocess.CREATE_NO_WINDOW
                     | getattr(subprocess, "DETACHED_PROCESS", 0),
                     close_fds=True)


def disk_ok(size):
    return free_bytes() > size * FREE_FACTOR
