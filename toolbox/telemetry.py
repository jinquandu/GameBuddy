# -*- coding: utf-8 -*-
"""客户端统计遥测参数(REQ-20261028-002 方案 A)。

背景:客户端原先从不把程序版本发出去,服务端无从知道"多少人在用、
各是什么 build"。最轻的补法不是新增上报接口,而是在**已有的两个定时
轮询**(规则 6h / 更新 6h)URL 上追加查询参数——nginx 静态 alias 直出
时忽略 query,客户端拿到的响应完全不变,服务端 access_log 多记一行。

原则:
- 只带 build/版本/平台三个**非身份**字段;绝不带手机号、device_id、
  token(统计日志不得变成身份库);
- 服务端可忽略未知 query(旧服务端照常返回文件);
- 任何异常都不影响轮询——拼参数失败就退回不带参数的 URL。
"""
import sys

from toolbox.version import BUILD, VERSION


def query():
    """-> "build=20261028-r16&v=0.1&os=win"(纯 ASCII,无需转义)。"""
    osname = "win" if sys.platform == "win32" else (
        "mac" if sys.platform == "darwin" else sys.platform)
    return f"build={BUILD}&v={VERSION}&os={osname}"


def with_query(url):
    """给 URL 追加统计参数;已有 query 用 & 连接;空 URL 原样返回。"""
    u = str(url or "").strip()
    if not u:
        return u
    sep = "&" if "?" in u else "?"
    return f"{u}{sep}{query()}"
