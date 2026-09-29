"""轻量文件日志：data/logs/<name>.log，1MB×3 轮转，按需懒加载。

用法：
    from toolbox.logs import get
    log = get("screenrec")          # -> data/logs/screenrec.log
    log.info("P%d 开始 %s", part, path)

数据根跟随 toolbox.config（TOOLBOX_DATA_DIR 环境变量 > config.yaml paths.data_dir）。
"""
import logging
from logging.handlers import RotatingFileHandler

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
