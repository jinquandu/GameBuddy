# -*- coding: utf-8 -*-
"""REQ-20261014-003 统一日志体系回归：dashijie.* 分级、文件双写、env 漂移重配。

只测 logs.get_logger/reset_logging 的契约语义（与服务端 common.py 同语义）：
- stderr 仅 WARNING+；TOOLBOX_LOG_DIR 开文件双写（INFO+）；
- env 晚设置/变更触发自动重配（漂移检测）；reset_logging 清空 handler。
"""
import io
import logging
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, ".")
os.environ.pop("TOOLBOX_LOG_DIR", None)

from toolbox import logs


def handlers_of(ns="dashijie"):
    root = logging.getLogger(ns)
    return [(type(h).__name__, getattr(h, "level", None)) for h in root.handlers]


def capture_stderr(fn):
    """跑 fn 并捕获 dashijie 命名空间 stderr handler 的输出。

    StreamHandler 创建时已绑定 sys.stderr，redirect_stderr 换不掉——直接
    临时替换 handler.stream（等价于捕获该 handler 的全部写出）。"""
    sh = next(h for h in logging.getLogger("dashijie").handlers
              if isinstance(h, logging.StreamHandler)
              and not isinstance(h, logging.FileHandler))
    buf = io.StringIO()
    old, sh.stream = sh.stream, buf
    try:
        fn()
    finally:
        sh.stream = old
    return buf.getvalue()


def main():
    logs.reset_logging()
    log = logs.get_logger("test")

    # 1) 未设 TOOLBOX_LOG_DIR：仅 stderr(WARNING+)，INFO 不上屏
    assert log.name == "dashijie.test", log.name
    assert logging.getLogger("dashijie").propagate is False
    hs = handlers_of()
    assert hs == [("StreamHandler", logging.WARNING)], hs
    out = capture_stderr(lambda: (log.info("info-不应出现"),
                                  log.warning("warn-应出现")))
    assert "info-不应出现" not in out
    assert "warn-应出现" in out

    # 2) env 晚设置：漂移检测自动重配，文件 sink 出 INFO+
    with tempfile.TemporaryDirectory() as td:
        os.environ["TOOLBOX_LOG_DIR"] = td
        log2 = logs.get_logger("test")          # 触发漂移重配
        hs = handlers_of()
        kinds = sorted(k for k, _ in hs)
        assert kinds == ["StreamHandler", "TimedRotatingFileHandler"], hs
        flevels = [lvl for k, lvl in hs if k == "TimedRotatingFileHandler"]
        assert flevels == [logging.INFO], flevels
        out = capture_stderr(lambda: (log2.info("file-应有"),
                                      log2.error("err-两端都有")))
        content = (Path(td) / "dashijie.log").read_text(encoding="utf-8")
        assert "file-应有" in content and "err-两端都有" in content, content
        assert "file-应有" not in out               # INFO 仍不上屏
        assert "err-两端都有" in out
        logs.reset_logging()        # 关文件句柄（Windows 下临时目录才能删）

    # 3) reset_logging：handler 清空、可重配
    os.environ.pop("TOOLBOX_LOG_DIR", None)
    logs.reset_logging()
    assert handlers_of() == []
    logs.get_logger("test")
    assert handlers_of() == [("StreamHandler", logging.WARNING)]

    print("logs.get_logger 语义回归 ✓（stderr 分级 / 文件双写 / env 漂移重配）")


if __name__ == "__main__":
    main()
