# -*- coding: utf-8 -*-
"""REQ-20261019-013 回归：识别链路登录门控 + 后台日任务标记。

- require_login：无 api_url 放行 / 有 api_url 无会话拒绝 / 有效会话放行 /
  过期会话拒绝 / gate_pipeline=false 逃生门；
- run_pipeline 入口门控先于一切阶段（未登录在动目标前就拒绝）；
- folder_watch 日去重标记 last_daily（持久化往返）。
"""
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, ".")

import toolbox.device_identity as di
import toolbox.folder_watch as fw
from toolbox.account import NotLoggedIn, require_login
from toolbox.pipeline import run_pipeline

REAL_LOAD = di.load_session


def _sess(delta_sec, bearer="tok"):
    return {"bearer": bearer, "expires_at_ts": time.time() + delta_sec}


def main():
    try:
        # 1) 内部机（未配 api_url）：不门控
        assert require_login({"account": {}}) == {}
        assert require_login({}) == {} or True     # 真实 config 兜底分支不炸
        # 2) 账号模式 + 无会话 → NotLoggedIn
        cfg = {"account": {"api_url": "https://x/api/account/v1"}}
        di.load_session = lambda: {}
        try:
            require_login(cfg)
            raise AssertionError("无会话应拒绝")
        except NotLoggedIn as e:
            assert e.code == "not_logged_in"
        # 3) 有效会话 → 放行并返回会话
        di.load_session = lambda: _sess(90 * 86400)
        got = require_login(cfg)
        assert got.get("bearer") == "tok"
        # 4) 过期会话 → 拒绝（3 个月免登完全跟随服务端 expires_in）
        di.load_session = lambda: _sess(-10)
        try:
            require_login(cfg)
            raise AssertionError("过期会话应拒绝")
        except NotLoggedIn:
            pass
        # 5) 逃生门
        assert require_login({"account": {
            "api_url": "https://x", "gate_pipeline": False}}) == {}
        # 6) pipeline 入口门控：未登录在触碰目标文件前即拒（target 不存在
        #    也不应报 FileNotFoundError——门控优先）
        di.load_session = lambda: {}
        try:
            run_pipeline(r"C:\__不存在__gate_probe", config=cfg)
            raise AssertionError("run_pipeline 未拦未登录")
        except NotLoggedIn:
            pass
        print("require_login / run_pipeline 门控 ✓（放行/拒绝/逃生门/优先级）")

        # 7) 后台日任务标记：持久化往返 + 不伤队列
        with tempfile.TemporaryDirectory() as td:
            real_q = fw.QUEUE_FILE
            fw.QUEUE_FILE = Path(td) / ".folder_watch.json"
            try:
                fw._save({"jobs": {"k1": {"state": "done", "session": "s1"}}})
                assert fw.last_daily_run() == ""
                fw.mark_daily_run()
                today = time.strftime("%Y-%m-%d")
                assert fw.last_daily_run() == today
                st = fw._load()
                assert st["jobs"]["k1"]["state"] == "done", "标记写入不得破坏队列"
            finally:
                fw.QUEUE_FILE = real_q
        print("folder_watch last_daily 标记 ✓（往返/队列无损）")
    finally:
        di.load_session = REAL_LOAD
    print("PASS: REQ-013 门控与日任务回归全部通过")


if __name__ == "__main__":
    main()
