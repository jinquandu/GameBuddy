# -*- coding: utf-8 -*-
"""REQ-20261014-004 同源契约函数回归：toolbox/common.py（双端 md5 一致）。

- get_logger/reset_logging（服务端版实现逐字收敛）：
  - stderr 仅 WARNING+；TOOLBOX_LOG_DIR 开按天文件 sink（toolbox-YYYYMMDD.log，
    INFO+）并自动清理 30 天前旧档；目录建不出来降级仅 stderr；
  - env 晚设置/变更触发自动重配（漂移检测）；reset 清空 handler。
- slug/mmss：与既有 naming 薄转发一致、契约格式不变；
- du：文件/目录/列表累计字节数；
- collect_videos：目录按分片序（P2 在 P10 前）；单文件直返。
"""
import io
import logging
import os
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, ".")
os.environ.pop("TOOLBOX_LOG_DIR", None)
os.environ.pop("TOOLBOX_LOG_LEVEL", None)
os.environ.pop("TOOLBOX_LOG_VERBOSE", None)

from toolbox import common


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
    common.reset_logging()
    log = common.get_logger("test")

    # 1) 未设 TOOLBOX_LOG_DIR：仅 stderr(WARNING+)，INFO 不上屏
    assert log.name == "dashijie.test", log.name
    assert logging.getLogger("dashijie").propagate is False
    hs = handlers_of()
    assert hs == [("StreamHandler", logging.WARNING)], hs
    out = capture_stderr(lambda: (log.info("info-不应出现"),
                                  log.warning("warn-应出现")))
    assert "info-不应出现" not in out
    assert "warn-应出现" in out

    # 2) env 晚设置：漂移检测自动重配，按天文件 sink 出 INFO+ 并清过期旧档
    with tempfile.TemporaryDirectory() as td:
        today = Path(td) / f"toolbox-{time.strftime('%Y%m%d')}.log"
        stale = Path(td) / "toolbox-20200101.log"       # 远超保留期
        stale.write_text("old", encoding="utf-8")
        # 过期判定按 mtime（非文件名日期），回拨时间戳模拟 40 天前
        old_ts = time.time() - 40 * 86400
        os.utime(stale, (old_ts, old_ts))
        os.environ["TOOLBOX_LOG_DIR"] = td
        log2 = common.get_logger("test")                # 触发漂移重配
        hs = handlers_of()
        kinds = sorted(k for k, _ in hs)
        assert kinds == ["FileHandler", "StreamHandler"], hs
        flevels = [lvl for k, lvl in hs if k == "FileHandler"]
        assert flevels == [logging.INFO], flevels
        out = capture_stderr(lambda: (log2.info("file-info-应落盘"),
                                      log2.error("err-两端都有")))
        content = today.read_text(encoding="utf-8")
        assert "file-info-应落盘" in content and "err-两端都有" in content, content
        assert "file-info-应落盘" not in out                # INFO 仍不上屏
        assert "err-两端都有" in out
        assert not stale.exists()                       # 配置时清理过期旧档
        common.reset_logging()      # 关文件句柄（Windows 下临时目录才能删）

    # 2b) 目录建不出来（TOOLBOX_LOG_DIR 指到普通文件）：降级仅 stderr
    with tempfile.TemporaryDirectory() as td:
        blocker = Path(td) / "blocker"
        blocker.write_text("x", encoding="utf-8")
        os.environ["TOOLBOX_LOG_DIR"] = str(blocker)
        common.get_logger("test")
        assert [k for k, _ in handlers_of()] == ["StreamHandler"], handlers_of()
        common.reset_logging()

    # 3) reset_logging：handler 清空、可重配；默认 name="toolbox"
    os.environ.pop("TOOLBOX_LOG_DIR", None)
    common.reset_logging()
    assert handlers_of() == []
    assert common.get_logger().name == "dashijie.toolbox"
    assert handlers_of() == [("StreamHandler", logging.WARNING)]

    # 4) 契约函数：slug/mmss 与旧实现（naming 薄转发）一致；du 按字节累计
    from toolbox import naming
    cases = ["钢牙/小晨 香菜:第2场", "a/b\\c d|e?f*g\"h'i", "  ", "x"]
    for s in cases:
        assert common.slug(s) == naming.slug(s), s
    assert common.slug("钢牙小晨国庆特别篇超长场次名", 10) \
        == naming.slug("钢牙小晨国庆特别篇超长场次名", 10)
    assert common.mmss(125) == "02m05s" and common.mmss(0) == "00m00s"
    with tempfile.TemporaryDirectory() as td:
        f = Path(td) / "a.bin"
        f.write_bytes(b"12345")
        sub = Path(td) / "sub"
        sub.mkdir()
        (sub / "b.bin").write_bytes(b"1234567")
        assert common.du(f) == 5
        assert common.du(td) == 12
        assert common.du([f, sub]) == 12

    # 5) collect_videos：目录按分片序（P2 在 P10 前）；单文件直返
    with tempfile.TemporaryDirectory() as td:
        t = Path(td)
        for n in ("P10.mp4", "P2.flv", "P1.mp4", "note.txt"):
            (t / n).write_bytes(b"")
        vids = common.collect_videos(t)
        assert [v.name for v in vids] == ["P1.mp4", "P2.flv", "P10.mp4"], vids
        single = common.collect_videos(t / "P1.mp4")
        assert [v.name for v in single] == ["P1.mp4"]

    print("common 契约函数回归 ✓（stderr 分级 / 按天文件+过期清理 / 降级 / "
          "env 漂移重配 / slug-mmss-du / collect_videos 分片序）")


if __name__ == "__main__":
    main()
