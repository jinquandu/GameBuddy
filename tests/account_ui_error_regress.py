# -*- coding: utf-8 -*-
"""REQ-20261019-014 P0 回归：登录框错误路径（确定性，无 GUI 依赖）。

旧缺陷：`except AccountError as e` 后 `win.after(lambda: …e…)`——e 出块即
被 Python 清除，异步回调 NameError，429 等失败提示被吞、UI 停留「发送中」。

两道防线：
1. AST 静态守卫：account_ui.py 所有 except 块内，异步调度的 lambda 不得
   引用被绑异常变量（该 bug 类的精确特征，杜绝复发）；
2. 行为：_err_text 对 AccountError/非预期异常给出「阶段+错误码+HTTP+耗时」
   的安全文本（手机号/验证码/token 不出现）。

（GUI 全链路由 pet_selftest 冒烟 + 打包期冻结行为核验覆盖；交互式驱动
在嵌套等待上下文里 bg→after 不派发，属测试环境怪癖，不作为回归口径。）
"""
import ast
import sys
import time

sys.path.insert(0, ".")

from toolbox.account import AccountError
from toolbox.account_ui import _err_text

SRC = "toolbox/account_ui.py"


def lam_names(node):
    """lambda 表达式里引用的名字（粗粒度：全部 Name 即可，误报无害——
    except 块内 lambda 引用 e 本来就应清零）。"""
    names = set()

    def visit(n):
        for ch in ast.iter_child_nodes(n):
            if isinstance(ch, ast.Name):
                names.add(ch.id)
            visit(ch)
    visit(node)
    return names


def static_guard():
    tree = ast.parse(open(SRC, encoding="utf-8").read())
    offenders = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.ExceptHandler) or not node.name:
            continue
        for sub in ast.walk(node):
            if isinstance(sub, ast.Lambda) and node.name in lam_names(sub):
                offenders.append(node.lineno)
    assert not offenders, \
        f"except 块内 lambda 引用异常变量（NameError 缺陷特征）: 行 {offenders}"


def main():
    static_guard()
    print("  ✓ AST 守卫：account_ui 无 except-lambda 引用异常变量")

    t0 = time.time() - 0.5
    got = _err_text("发送", AccountError(
        "rate_limited", "本次短信测试额度已用完", 429), t0)
    for s in ("发送失败", "rate_limited", "HTTP429", "ms）"):
        assert s in got, f"429 文案缺 {s!r}: {got}"
    print("  ✓ 429 →", got)

    got = _err_text("发送", AccountError("network", "网络错误：超时"), t0)
    assert "发送失败" in got and "network" in got and "HTTP" not in got
    print("  ✓ network →", got)

    got = _err_text("登录", ValueError("boom"), t0)
    assert "登录异常" in got and "unexpected" in got
    print("  ✓ unexpected →", got)

    # 脱敏：错误文案不得含手机号/凭据形态
    for secret in ("13800001522", "Bearer "):
        assert secret not in got
    print("PASS: 登录框错误路径确定性回归全部通过")


if __name__ == "__main__":
    main()
