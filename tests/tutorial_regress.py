# -*- coding: utf-8 -*-
"""REQ-20261022-002 回归:新手教程(沙盒/拦截/守卫/C2S-16 客户端/清理)。

- 沙盒:plant_sandbox 落「新手体验-demo/」到监控目录;未配目录返回 None;
- 队列拦截:folder_watch 消费教程会话不跑识别链,直接 done;
- 守卫:教程消费路径不写 last_production_ts(不占 24h 窗口);
- 素材:material_ready SHA-256 校验(假内容拒绝);
- C2S-16 客户端:429/404/正常三态映射。
"""
import json
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, ".")

from toolbox import folder_watch as fw, tutorial as tut
from toolbox import uploader as up


def main():
    ok = 0

    def check(cond, what):
        nonlocal ok
        assert cond, "FAIL: " + what
        ok += 1
        print("  ✓ " + what)

    with tempfile.TemporaryDirectory() as td:
        t = Path(td)
        real_q = fw.QUEUE_FILE
        fw.QUEUE_FILE = t / "state.json"
        real_tut = tut.TUTORIAL_DIR
        tut.TUTORIAL_DIR = t / "tutorial"
        tut.sandbox_dir  # noqa
        try:
            # ---- 素材校验(伪造内容拒绝) ----
            tut.TUTORIAL_DIR.mkdir(parents=True, exist_ok=True)
            fake = tut.material_path()
            fake.write_bytes(b"not-a-video")
            check(not tut.material_ready(), "素材 SHA-256 校验:伪造内容拒绝")

            # ---- 沙盒落位(素材伪造:临时改校验目标为假内容摘要) ----
            fake_sha = __import__("hashlib").sha256(b"not-a-video").hexdigest()
            real_sha = tut.DEMO_SHA256
            tut.DEMO_SHA256 = fake_sha
            wdir = t / "watch"
            wdir.mkdir()
            try:
                dst = tut.plant_sandbox(watch_dir=str(wdir))
                check(dst is not None and dst.name == tut.TUTORIAL_SESSION,
                      "沙盒落位=监控目录/新手体验-demo")
                check((dst / "新手体验-demo.mp4").exists(), "素材已复制进沙盒")
                check(tut.plant_sandbox(watch_dir="") is None,
                      "无目录 → None")
            finally:
                tut.DEMO_SHA256 = real_sha

            # ---- 队列:扫描入队 → 消费拦截(不跑识别链) ----
            import os
            v = dst / "新手体验-demo.mp4"
            os.utime(v, (time.time() - 3600, time.time() - 3600))  # 拨旧=稳定
            # 真实场次与教程会话并存(用户反馈场景:教程不拉起真实场次)
            _mkvid = wdir / "真实场次A"
            _mkvid.mkdir()
            (_mkvid / "P1.mp4").write_bytes(b"v" * 2048)
            os.utime(_mkvid / "P1.mp4",
                     (time.time() - 3600, time.time() - 3600))
            n = fw.scan(wdir, state=fw._load())
            check(n == 2, f"教程会话+真实场次双入队（实得 {n}）")
            j = fw.claim_next(only_session=tut.TUTORIAL_SESSION)
            check(j["session"] == tut.TUTORIAL_SESSION, "领取=教程会话")
            fw.finish(j["key"], True, "新手教程会话(服务端短路)")
            check(fw.last_production_ts() == 0,
                  "守卫:教程不写 last_production_ts(不占 24h 窗口)")

            # ---- only_session 隔离(2026-10-22 用户反馈:教程不拉起真实场次) ----
            queued_before = fw.summary()["queued"]
            n2, results2 = fw.process_pending(
                wdir, runner=lambda pth: (_ for _ in ()).throw(
                    AssertionError("教程消费不应起识别链子进程")),
                only_session=tut.TUTORIAL_SESSION)
            check(not results2 and fw.summary()["queued"] == queued_before,
                  "only_session:教程已 done,真实场次留在队列不被消费")
            n2, results2 = fw.process_pending(
                wdir, runner=lambda pth: (_ for _ in ()).throw(
                    AssertionError("教程消费不应起识别链子进程")),
                only_session=tut.TUTORIAL_SESSION)
            # 教程会话已 done;only_session 下不应消费真实场次
            check(all(s != "真实场次A" for s, _, _ in results2)
                  and fw.summary()["queued"] == queued_before,
                  "only_session:教程消费不触碰真实场次(留在队列)")
            # 不带过滤的正常消费仍能领到真实场次(真实链路不受影响)
            j3 = fw.claim_next()
            check(j3 is not None and j3["session"] == "真实场次A",
                  "无过滤时真实场次正常消费")

            # ---- C2S-16 客户端三态(方法级 mock) ----
            import toolbox.account as acct
            real_login = acct.require_login
            acct.require_login = lambda config=None: {"bearer": "t"}
            orig_req = up.ProductionUploader._request

            class FakeResp(dict):
                pass

            def mk(code=200, payload=None):
                def _req(self, method, path, body=None, data=None,
                         timeout=600):
                    if code == 200:
                        return payload
                    raise up.UploadError("x", "m", code)
                return _req

            try:
                up.ProductionUploader._request = mk(200, {
                    "tutorial": True,
                    "produce": {"state": "done", "works_url": "/eval/demo/preview/x"}})
                r = tut.call_tutorial_api("http://mock/api/production/v1")
                check(r["tutorial"] is True and r["produce"]["works_url"]
                      == "/eval/demo/preview/x", "C2S-16 正常响应解析")
                up.ProductionUploader._request = mk(429)
                try:
                    tut.call_tutorial_api("http://mock/api/production/v1")
                    raised = "none"
                except Exception as e:
                    raised = getattr(e, "code", type(e).__name__)
                check(raised == "x", "429 超限透传(客户端按账号错误展示)")
                from toolbox.tutorial import TutorialNotAvailable
                up.ProductionUploader._request = mk(404)
                try:
                    tut.call_tutorial_api("http://mock/api/production/v1")
                    raised = "none"
                except TutorialNotAvailable:
                    raised = "TNA"
                except Exception as e:
                    raised = type(e).__name__
                check(raised == "TNA", "旧服务端 404 → TutorialNotAvailable(降级)")
            finally:
                up.ProductionUploader._request = orig_req
                acct.require_login = real_login

            # ---- 清理/重放 ----
            tut.mark_done()
            check(tut.is_done(), "完成标记")
            # done 后仅保留 1 天:jobs_view/summary 次日隐藏+就地清理
            st = fw._load()
            for jj in st["jobs"].values():
                if jj["session"] == tut.TUTORIAL_SESSION:
                    jj["done_at"] = time.time() - fw.TUTORIAL_KEEP_S - 10
                    jj["state"] = "done"
            fw._save(st)
            check(all(j["session"] != tut.TUTORIAL_SESSION
                      for j in fw.jobs_view()),
                  "过期教程任务从列表隐藏")
            check(fw.summary()["done"] == 0, "过期教程不计入 summary")
            check(not (dst / "新手体验-demo").exists(),
                  "过期清理:沙盒目录已删")
            tut.reset(watch_dir=str(wdir))
            check(not tut.is_done() and not (dst / "新手体验-demo.mp4").exists()
                  and not tut.material_path().exists()
                  and not (dst / "新手体验-demo").exists(),
                  "reset:沙盒+素材+标记清空(可重放)")
        finally:
            fw.QUEUE_FILE = real_q
    print("PASS: " + str(ok) + " 项断言全部通过")


if __name__ == "__main__":
    main()
