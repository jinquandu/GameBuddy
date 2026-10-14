"""轻量文件日志：data/logs/<name>.log，1MB×3 轮转，按需懒加载。

用法：
    from toolbox.logs import get
    log = get("screenrec")          # -> data/logs/screenrec.log
    log.info("P%d 开始 %s", part, path)

数据根跟随 toolbox.config（TOOLBOX_DATA_DIR 环境变量 > config.yaml paths.data_dir）。

统一日志体系（REQ-20261014-003，get_logger/reset_logging）：与服务端
toolbox/common.py 同语义。服务端落在 common.py、端侧落在本模块——端侧无
common.py（slug/mmss 契约函数单一实现在 naming.py，不为此复制）：
    from toolbox.logs import get_logger
    log = get_logger(__name__)      # -> dashijie.toolbox.xxx
- stderr 只出 WARNING+（不挤占契约 C2S-4/5 失败回显的 800 字 stderr 窗口）；
- 设 TOOLBOX_LOG_DIR 时文件双写（INFO+，按天轮转、保留 30 天；打标站注入
  <EVAL_ROOT>/logs）；未设置退化为仅 stderr，print 输出与契约回显不变；
- get_logger 自带 env 漂移检测：TOOLBOX_LOG_DIR 晚设置/变更时自动重配；
  reset_logging() 强制清 handler（测试隔离用）。
"""
import logging
import os
from logging.handlers import RotatingFileHandler, TimedRotatingFileHandler
from pathlib import Path

from toolbox.config import DATA

_cache = {}


def get(name: str) -> logging.Logger:
    """按名字取（并按需创建）文件 logger；同名复用同一实例。"""
    lg = _cache.get(name)
    if lg is not None:
        return lg
    lg = logging.getLogger(f"toolbox.{name}")
    lg.setLevel(logging.INFO)
    if not lg.handlers:                     # 防重复添加（重复 import 场景）
        d = DATA / "logs"
        d.mkdir(parents=True, exist_ok=True)
        h = RotatingFileHandler(d / f"{name}.log", maxBytes=1_000_000,
                                backupCount=3, encoding="utf-8")
        h.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s",
                                         "%Y-%m-%d %H:%M:%S"))
        lg.addHandler(h)
        lg.propagate = False
    _cache[name] = lg
    return lg


# ---------------------------------------------------------------- 统一日志（dashijie.*）

_NS = "dashijie"
_FMT = "%(asctime)s %(levelname)s %(name)s %(message)s"
_log_dir_applied = None      # 已应用的 TOOLBOX_LOG_DIR 值；None = 尚未配置过


def reset_logging():
    """拆除 dashijie.* 既有 handler（测试隔离 / 外部改 env 后强制重配）。"""
    global _log_dir_applied
    root = logging.getLogger(_NS)
    for h in list(root.handlers):
        root.removeHandler(h)
        h.close()
    _log_dir_applied = None


def _configure():
    global _log_dir_applied
    want = os.environ.get("TOOLBOX_LOG_DIR") or ""
    if _log_dir_applied is not None and want != _log_dir_applied:
        reset_logging()                      # env 晚设置/变更：换文件 sink
    if _log_dir_applied is not None:
        return
    root = logging.getLogger(_NS)
    root.setLevel(logging.INFO)
    root.propagate = False                   # 不冒泡给 root（打标站 app 自管）
    sh = logging.StreamHandler()             # 默认流即 sys.stderr
    sh.setLevel(logging.WARNING)
    sh.setFormatter(logging.Formatter(_FMT, "%Y-%m-%d %H:%M:%S"))
    root.addHandler(sh)
    if want:
        d = Path(want)
        d.mkdir(parents=True, exist_ok=True)
        fh = TimedRotatingFileHandler(d / "dashijie.log", when="midnight",
                                      backupCount=30, encoding="utf-8")
        fh.setLevel(logging.INFO)
        fh.setFormatter(logging.Formatter(_FMT, "%Y-%m-%d %H:%M:%S"))
        root.addHandler(fh)
    _log_dir_applied = want


def get_logger(name=None):
    """统一日志入口：dashijie.*（name 为子命名空间，习惯传 __name__）。"""
    _configure()
    return logging.getLogger(f"{_NS}.{name}" if name else _NS)
