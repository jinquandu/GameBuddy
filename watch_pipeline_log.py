# -*- coding: utf-8 -*-
"""pipeline.log 实时查看器（纯呈现侧，不改写入/不改文件）。

修复 pet 收跑链子进程输出的 GBK→UTF-8 误解码残留：
- 子进程按 GBK 打印、按 UTF-8 解码入档后，一部分字变成拉丁字符（可逆），
  一部分变成 U+FFFD（信息已丢，显示 □）。
- 逐行在「原文 / utf-8→gbk 回转修复」两候选中选 CJK 更多者；
  U+FFFD 一律先替换为 □，避免修复后出现假的「锟斤拷」。

用法: py watch_pipeline_log.py [日志路径]     # 默认 dist/DashijiePet/data/logs/pipeline.log
Ctrl+C 退出。
"""
import sys
import time
from pathlib import Path

DEFAULT = Path(r"dist/DashijiePet/data/logs/pipeline.log")


def _cjk_count(s):
    return sum(1 for c in s if "\u4e00" <= c <= "\u9fff")


def repair(line):
    """单行乱码修复（字节级，能救多少救多少）：

    子进程 GBK 输出被按 UTF-8 解码入档后，成功误解码的字符 encode 回
    UTF-8 恰是原 GBK 字节，可整行按 GBK 解回；信息真正丢在 U+FFFD 处
    （原字节已不可知）——以 NUL 占位保住后续字节的 GBK 配对对齐，
    最后替换为 □。原样与修复两个候选取 CJK 多者。
    """
    if not line.strip():
        return line
    try:
        cand = (line.encode("utf-8")
                    .replace(b"\xef\xbf\xbd", b"\x00")
                    .decode("gbk", errors="replace")
                    .replace("\x00", "□"))
    except (UnicodeEncodeError, UnicodeDecodeError):
        cand = line
    orig = line.replace("\ufffd", "□")
    return cand if _cjk_count(cand) > _cjk_count(orig) else orig


def follow(path):
    path = Path(path)
    if not path.is_file():
        print(f"日志不存在: {path}")
        sys.exit(1)
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        for l in f.readlines()[-20:]:          # 开场先回放末 20 行（已修复）
            print(repair(l), end="", flush=True)
        while True:
            line = f.readline()
            if line:
                print(repair(line), end="", flush=True)
            else:
                time.sleep(0.5)


if __name__ == "__main__":
    follow(sys.argv[1] if len(sys.argv) > 1 else DEFAULT)
