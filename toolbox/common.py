"""跨链路共享的契约函数（单一来源）。

slug()：目录/文件名规范化的唯一实现。knockdowns/pickups/voice/reports 的
数据目录名与切片文件名都由它决定，应用端 push 与服务端 import-session
按同样的名字找数据——改这里即改两端目录契约，勿在别处复制。
（asr.py 的 _slug 是另一套更严格的转写目录命名，语义不同，不在此收敛。）

mmss()：秒 -> `MMmSSs` 切片文件名时间戳段（voice 切片命名 §5.7；端侧
knockdown 切片同式，端侧本定义在 knockdown._mmss，服务端子集不含该模块，
故收敛到此处作为单一实现）。

du()：路径体积统计，transfer（客户端上传清单）与打标站（磁盘占用展示）共用。

get_logger()：统一日志（打标站/CLI/混剪共用，2026-10-14）。
- 双 sink：stderr 常开（默认 WARNING+；契约 C2S-4/5 失败时 stderr 截 800
  字回显端侧，INFO 刷 stderr 会把真报错挤出窗口，故 INFO 只进文件）；
  文件 sink 由 TOOLBOX_LOG_DIR 显式开启（打标站注入 <EVAL_ROOT>/logs，
  子进程经环境继承同一 sink），INFO+，按天一个文件，保留 30 天自动清理。
- 目录建不出来（本地开发 EVAL_ROOT 不存在等）自动降级为仅 stderr，
  绝不让日志问题拖垮业务。

collect_videos()：目标（目录/文件/登记id）-> 按分片序排序的视频路径列表。
transfer.discover_session 双端共用（2026-10-16 REQ-20261014-004 自端侧
events.py 归位至此；端侧 events/knockdown/pickup 等经 toolbox.events 转引）。
登记 id -> recording_path 的兜底依赖端侧独有模块 toolbox.ingest，懒加载
不触发即无碍（服务端不调 discover_session）。
"""
import logging
import os
import re
import sys
import time
from pathlib import Path

_BY_SLUG = re.compile(r"[/\\\s:|\"'?*]+")

LOG_NAMESPACE = "dashijie"     # 日志命名空间根（与代码包名解耦）
LOG_RETAIN_DAYS = 30

_cfg_dir = None                # 当前已配置的文件 sink 目录（漂移检测用）


def _log_dir():
    """文件 sink 目录；未配置或建不出来（权限/路径不存在）返回 None 降级。"""
    raw = os.environ.get("TOOLBOX_LOG_DIR", "")
    if not raw:
        return None
    try:
        p = Path(raw)
        p.mkdir(parents=True, exist_ok=True)
        return p if os.access(p, os.W_OK) else None
    except OSError:
        return None


def _prune_old_logs(d):
    cutoff = time.time() - LOG_RETAIN_DAYS * 86400
    for f in d.glob("toolbox-*.log"):
        try:
            if f.stat().st_mtime < cutoff:
                f.unlink()
        except OSError:
            pass


def get_logger(name="toolbox"):
    """取 dashijie.<name> logger；首次调用或文件 sink 目录变化时（重）配置根。

    多进程（gunicorn worker / recalc/remix 子进程）追加写同一按天文件，
    行级 write+O_APPEND 原子性对本站流量量级够用；进程跨零点继续写
    打开当日的文件（内部工具可接受）。

    漂移检测：模块可能在 TOOLBOX_LOG_DIR 设定之前就被 import 并完成
    首次配置（如 app.py 先 import toolbox.douyin 再注入默认 sink），
    之后任何一次 get_logger 都会按当前 env 重建 handler——保证"晚设置
    的 sink 也能生效"。
    """
    global _cfg_dir
    want = os.environ.get("TOOLBOX_LOG_DIR", "")
    root = logging.getLogger(LOG_NAMESPACE)
    if root.handlers and want != _cfg_dir:
        reset_logging()
    if not root.handlers:
        fmt = logging.Formatter(
            "%(asctime)s %(levelname)s [%(name)s] %(message)s",
            "%Y-%m-%d %H:%M:%S")
        root.setLevel(logging.DEBUG)
        sh = logging.StreamHandler(sys.stderr)
        # stderr 只出 WARNING+：CLI 失败回显窗口只有 800 字，留给真报错
        sh.setLevel(logging.DEBUG if os.environ.get("TOOLBOX_LOG_VERBOSE")
                    else logging.WARNING)
        sh.setFormatter(fmt)
        root.addHandler(sh)
        d = _log_dir()
        if d:
            fh = logging.FileHandler(
                d / f"toolbox-{time.strftime('%Y%m%d')}.log",
                encoding="utf-8")
            fh.setLevel(os.environ.get("TOOLBOX_LOG_LEVEL", "INFO").upper())
            fh.setFormatter(fmt)
            root.addHandler(fh)
            _prune_old_logs(d)
        root.propagate = False
        _cfg_dir = want
    return logging.getLogger(f"{LOG_NAMESPACE}.{name}")


def reset_logging():
    """清掉根 logger 的 handler 与状态（测试隔离用，业务代码勿调）。"""
    global _cfg_dir
    _cfg_dir = None
    root = logging.getLogger(LOG_NAMESPACE)
    for h in list(root.handlers):
        root.removeHandler(h)
        h.close()


def slug(s, limit=40):
    return _BY_SLUG.sub("_", s)[:limit].strip("_") or "clip"


def mmss(t):
    """秒 -> `MMmSSs`（与端侧 knockdown._mmss 同式，勿改格式）。"""
    m, s = divmod(int(round(t)), 60)
    return f"{m:02d}m{s:02d}s"


def du(paths):
    """单路径或路径列表的总字节数（目录递归）。"""
    if isinstance(paths, (str, Path)):
        paths = [paths]
    total = 0
    for p in map(Path, paths):
        if p.is_file():
            total += p.stat().st_size
        elif p.is_dir():
            total += sum(f.stat().st_size for f in p.rglob("*") if f.is_file())
    return total


# ---------------------------------------------------------------- 视频收集


def collect_videos(target):
    """目标（目录/文件/登记id）-> 按分片序排序的视频路径列表。"""
    p = Path(target).expanduser()
    if p.is_dir():
        vids = sorted((f.resolve() for f in p.iterdir()
                       if f.suffix.lower() in {".mp4", ".flv", ".mkv", ".mov"}),
                      key=lambda f: (len(f.stem), f.name))   # P2 排在 P10 前
        if not vids:
            raise RuntimeError(f"{p} 下没有视频文件")
        return vids
    if p.is_file():
        return [p.resolve()]
    from toolbox.ingest import recording_path   # 未登记则抛 FileNotFoundError
    return [recording_path(str(target))]
