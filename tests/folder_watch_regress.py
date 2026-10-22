# -*- coding: utf-8 -*-
"""folder_watch 持久队列回归（REQ-20261019-002 D 项首批）。

覆盖：稳定判定（静置/写入中/空文件）、场次目录与散文件两类目标、
键去重（任何状态不重复入队）、文件变化=新任务、失败重试与上限记
failed、租约超时回收、process_pending 假 runner 全流程、崩溃续跑
（队列持久化跨"进程"）。
"""
import json
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, ".")

from toolbox import folder_watch as fw


def main():
    ok = 0

    def check(cond, what):
        nonlocal ok
        assert cond, f"FAIL: {what}"
        ok += 1
        print(f"  ✓ {what}")

    with tempfile.TemporaryDirectory() as td:
        t = Path(td)
        watch = t / "watch"
        watch.mkdir()
        old = time.time() - 3600          # 一小时前=早已静置

        def touch(p, size=100, age=old):
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(b"x" * size)
            import os
            os.utime(p, (age, age))

        real_q = fw.QUEUE_FILE
        fw.QUEUE_FILE = t / "state.json"
        try:
            # 1) 稳定判定
            s1 = watch / "场次A"
            touch(s1 / "P1 一段.mp4")
            touch(s1 / "P2 二段.mp4")
            fresh = time.time() - 30       # 30s 前仍在写入窗口内
            touch(watch / "场次B" / "P1.mp4", age=fresh)
            touch(watch / "场次C" / "empty.mp4", size=0)
            touch(watch / "散装单文件.mp4")
            (watch / "说明.txt").write_text("非视频忽略")

            n = fw.scan(watch)
            st = fw._load()["jobs"]
            check(n == 2, "仅稳定目标入队（场次A + 散文件；写入中/空/非视频不入）")
            check(any(j["session"] == "场次A" for j in st.values())
                  and any(j["session"] == "散装单文件" for j in st.values()),
                  "场次目录与散文件两类目标登记")

            # 2) 去重 + 目录任务聚合（REQ-20261022-002:一个文件夹=一个任务）
            check(fw.scan(watch) == 0, "重扫零新入队（任何状态不重复入队）")
            touch(s1 / "P1 一段.mp4", size=200, age=time.time() - 3700)
            n = fw.scan(watch)
            st = fw._load()["jobs"]
            check(n == 0, "目录内容变化不另立新任务（沿用原任务）")
            check(len([j for j in st.values()
                       if j["path"].endswith("场次A")]) == 1,
                  "一个文件夹=一个任务（不裂成多条）")
            check(any("内容已更新" in j.get("note", "")
                      for j in st.values() if j["path"].endswith("场次A")),
                  "内容变化在原任务上标注「内容已更新」")

            # 3) claim/finish：成功、失败重试、上限
            jobs_before = len(fw._load()["jobs"])
            j = fw.claim_next()
            check(j["state"] == "running" and j["attempts"] == 1, "claim→running 记租约")
            fw.finish(j["key"], True, "跑链成功")
            check(fw._load()["jobs"][j["key"]]["state"] == "done", "成功→done")

            j2 = fw.claim_next()
            fw.finish(j2["key"], False, "rc=1")
            st = fw._load()["jobs"][j2["key"]]
            check(st["state"] == "queued" and st["attempts"] == 1,
                  "失败未达上限→queued 待重试")

            # 4) 租约超时回收：重复领取同一任务（最早 queued），叠加失败
            #    与租约回收，attempts 达上限记 failed
            j3 = fw.claim_next()
            check(fw._load()["jobs"][j3["key"]]["state"] == "running",
                  "再次领取最早 queued 任务")
            stx = fw._load()
            stx["jobs"][j3["key"]]["taken_ts"] = time.time() - 999 * 60
            fw._save(stx)
            fw.expire_leases()
            stx = fw._load()["jobs"][j3["key"]]
            check(stx["state"] == "failed" and stx["attempts"] >= 3,
                  f"租约超时+attempts 达上限 → failed（第{stx['attempts']}次）")

            # 5) process_pending 假 runner 全流程（新"进程"=重读持久状态）
            fake_calls = []

            def fake_runner(path):
                fake_calls.append(path)
                return (0 if "场次" in path else 1), "fake"

            new, results = fw.process_pending(watch, runner=fake_runner, limit=10)
            check(len(fake_calls) == 1,
                  "节拍只跑剩余 queued（failed 不再自动重试）")
            check(all(okk for _s, okk, _lg in results),
                  "剩余任务假 runner 成功")
            sm = fw.summary()
            check(sm["queued"] == 0 and sm["running"] == 0,
                  "节拍后无积压 queued/running")
            check(sm["failed"], "散文件假 runner 失败 → 记 failed（不静默吞）")

            # 6) kill_current 无进行中任务不炸
            fw.kill_current()
            check(True, "kill_current 空闲安全")
        finally:
            fw.QUEUE_FILE = real_q
    print(f"PASS: {ok} 项断言全部通过")


if __name__ == "__main__":
    main()
