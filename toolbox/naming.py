"""切片与目录命名约定（全局唯一实现，供全链共用）。

原寄生在 knockdown 的私有工具（_slug/_mmss，被 pickup/voice/transfer 跨模块
引用），2026-09-22 下沉为中性模块。注意：data/asr/ 的目录名是另一套历史
slug（asr.py 私有实现，保留中文且 n=48），为兼容既有数据不统一到这里。
"""
import re

_BY_SLUG = re.compile(r"[/\\\s:|\"'?*]+")


def slug(s, limit=40):
    return _BY_SLUG.sub("_", s)[:limit].strip("_") or "clip"


def mmss(t):
    m, s = divmod(int(round(t)), 60)
    return f"{m:02d}m{s:02d}s"
