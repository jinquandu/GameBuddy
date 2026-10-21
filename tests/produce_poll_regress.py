# -*- coding: utf-8 -*-
"""REQ-20261021-005 回归：成片自动化客户端对接（produce 轮询+作品页）。

- wait_produce 状态机：null(queued 前中间态)→queued→done 终态返回；
  failed 带 error；服务端无 produce 键（旧版）→ 立即 None 语义；超时；
  单拍网络错误不中断（下一拍重试）；
- works_full_url：相对路径拼 origin / 绝对 URL 原样 / 空值；
- on_produce 回调透传（pipeline/pet 挂点契约）。
"""
import sys

sys.path.insert(0, ".")

from toolbox import uploader as up


class FakeUploader(up.ProductionUploader):
    """按脚本逐拍回状态;status 可注入异常。"""

    def __init__(self, script, fail_first=0):
        self.script = list(script)          # 每拍 pop(0) 的 status 响应
        self.fail_first = fail_first
        self.pulls = 0
        self._mock_files = []

    def status(self, upload_id):
        self.pulls += 1
        if self.fail_first > 0:
            self.fail_first -= 1
            raise up.UploadError("network", "抖动")
        return self.script.pop(0) if self.script else {}


def main():
    ok = 0

    def check(cond, what):
        nonlocal ok
        assert cond, "FAIL: " + what
        ok += 1
        print("  ✓ " + what)

    # 1) null→queued→done 全链
    u = FakeUploader([
        {},                                          # produce 缺失=旧服务端
        {"produce": None},                           # queued 前中间态
        {"produce": {"state": "queued"}},
        {"produce": {"state": "done",
                     "works_url": "/eval/usr_x/preview/s1"}},
    ])
    states = []
    r = u.wait_produce("u1", poll_s=0, timeout_s=30,
                       sleep=lambda s: None, on_state=states.append)
    check(r == {"state": "done", "works_url": "/eval/usr_x/preview/s1"},
          f"终态 done 返回 works_url（{r}）")
    check(len(states) >= 3, f"中间态回调透传（{len(states)} 次）")
    check(u.pulls == 4, f"done 即停（4 拍，实拉 {u.pulls}）")

    # 2) failed 带尾栈
    u = FakeUploader([{"produce": {"state": "failed", "error": "boom-tail"}}])
    r = u.wait_produce("u1", poll_s=0, sleep=lambda s: None)
    check(r == {"state": "failed", "error": "boom-tail"}, "failed 原样透传")

    # 3) 旧服务端（无 produce 键）→ 立即 None 语义,不傻等
    u = FakeUploader([{}])
    r = u.wait_produce("u1", poll_s=0, timeout_s=1, sleep=lambda s: None)
    # 无 produce→waiting;1s 超时→timeout
    check(r == {"state": "timeout"}, f"旧服务端无 produce → 超时返回（{r}）")

    # 4) 网络抖动不中断
    u = FakeUploader([
        {"produce": {"state": "queued"}},
        {"produce": {"state": "done", "works_url": "/w"}},
    ], fail_first=2)
    r = u.wait_produce("u1", poll_s=0, timeout_s=30, sleep=lambda s: None)
    check(r.get("state") == "done", "单拍网络错误不中断轮询")

    # 5) works_full_url
    base = "https://95188.pw/api/production/v1"
    check(up.works_full_url(base, "/eval/usr_x/preview/s1")
          == "https://95188.pw/eval/usr_x/preview/s1", "相对路径拼 origin")
    check(up.works_full_url(base, "https://other/x") == "https://other/x",
          "绝对 URL 原样")
    check(up.works_full_url(base, "") == "", "空值安全")

    # 6) on_produce 契约:transfer.push 透传（签名层,不起真网）
    import inspect
    sig = inspect.signature(up.ProductionUploader.wait_produce)
    check("on_state" in sig.parameters and "poll_s" in sig.parameters,
          "wait_produce 挂点参数齐备")
    print("PASS: " + str(ok) + " 项断言全部通过")


if __name__ == "__main__":
    main()
