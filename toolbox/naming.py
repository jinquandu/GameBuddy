"""切片与目录命名约定的兼容转发（单一实现收敛 toolbox/common.py）。

slug/mmss 原寄生在 knockdown 私有工具（2026-09-22 下沉为本中性模块），
2026-10-16 随 REQ-20261014-004 双端结构对齐收敛进 common.py（与服务端
md5 一致）。本模块仅薄转发保历史导入方（events/knockdown/pickup/voice/
queues/测试），新代码请直接 `from toolbox.common import slug`。
注意：data/asr/ 的目录名是另一套历史 slug（asr.py 私有实现，保留中文且
n=48），为兼容既有数据不统一到这里。
"""
from toolbox.common import mmss, slug

__all__ = ["mmss", "slug"]
