# -*- coding: utf-8 -*-
"""工作台桌面录制×队列回归：录制产物（录像目录/<日期 桌面录制>/P*.mp4）
整体作为一个任务——同日多分P 只立一个目录任务；任务 done 后目录新增
分P → 重新排队（内容已更新）；pet 关闭路径先停录再退（逻辑层可测部分：
_rec_start 的目录选择回退 + ScreenRecorder 落盘位置 = watch_dir 下日期目录）。
UI 线程 marshaling 依赖真 mainloop，不在本测试范围。
"""
import os
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, ".")

from toolbox import folder_watch as fw

ok = 0


def check(cond, what):
    global ok
    assert cond, "FAIL: " + what
    ok += 1
    print("  ✓ " + what)


def _touch(path, size, mtime):
    path.write_bytes(b"x" * size)
    os.utime(path, (mtime, mtime))


def main():
    with tempfile.TemporaryDirectory() as td:
        t = Path(td)
        real_q = fw.QUEUE_FILE
        fw.QUEUE_FILE = t / "state.json"
        try:
            old = time.time() - 7200            # 判稳：mtime 已静置 2h
            # 模拟一天录制产物：watch/<日期 桌面录制>/P1 P2
            watch = t / "watch"
            day = watch / "2026-10-26 桌面录制"
            day.mkdir(parents=True)
            _touch(day / "2026-10-26 桌面录制 P1 26日20点01分.mp4", 3000, old)
            _touch(day / "2026-10-26 桌面录制 P2 26日20点31分.mp4", 3000, old)

            fw.scan(watch)
            jobs = fw._load()["jobs"]
            check(len(jobs) == 1, "同日两个分P 只立一个任务")
            j = list(jobs.values())[0]
            check(j["path"] == str(day), "任务路径=日期二级目录")
            check(j["session"] == "2026-10-26 桌面录制", "任务名=目录名")

            # 再扫幂等：不裂任务
            fw.scan(watch)
            check(len(fw._load()["jobs"]) == 1, "重复扫描不另立任务")

            # 任务 done 后目录新增 P3 → 重新排队（内容已更新）
            j["state"] = "done"
            fw._save(fw._load())
            _touch(day / "2026-10-26 桌面录制 P3 26日21点01分.mp4", 3000, old)
            fw.scan(watch)
            jobs = fw._load()["jobs"]
            check(len(jobs) == 1, "done 后目录增长仍只有一个任务")
            j2 = list(jobs.values())[0]
            check(j2["state"] == "queued", "done+新分P → 重新排队")
            check(j2.get("note") == "内容已更新", "标注内容已更新")

            # 次日新目录 → 新任务（每日一个新目录）
            day2 = watch / "2026-10-27 桌面录制"
            day2.mkdir()
            _touch(day2 / "2026-10-27 桌面录制 P1 27日20点01分.mp4", 3000, old)
            fw.scan(watch)
            jobs = fw._load()["jobs"]
            check(len(jobs) == 2, "次日新目录 = 新任务")
            check(any(j["path"] == str(day) for j in jobs.values())
                  and any(j["path"] == str(day2) for j in jobs.values()),
                  "两天的目录各自成任务")
        finally:
            fw.QUEUE_FILE = real_q

    # ---------- ScreenRecorder 落盘位置（不起 ffmpeg，只验目录规划）
    from toolbox import screenrec
    from toolbox.screenrec import ScreenRecorder
    with tempfile.TemporaryDirectory() as td:
        r = ScreenRecorder(Path(td) / "watch")
        check(r.root.name == "watch", "录制根=传入的录像目录")
        title = "2099-01-01 桌面录制"
        (Path(td) / "watch" / title).mkdir(parents=True)
        _touch(Path(td) / "watch" / title / f"{title} P2 x.mp4", 10, time.time())
        check(screenrec.next_part(Path(td) / "watch" / title, title) == 3,
              "同日续录 P 号续接（每日目录复用）")



    # ---------- 录制中×生产触发(REQ-20261026-001 口径完整链)
    # 录着的分P未过判稳期 -> 不计入待处理(不会录一半触发生产);
    # 静置判稳后 -> 整目录计入,超阈值即可触发。
    with tempfile.TemporaryDirectory() as td:
        t2 = Path(td)
        real_q2 = fw.QUEUE_FILE
        fw.QUEUE_FILE = t2 / "state.json"
        try:
            watch = t2 / "watch"
            day = watch / "2026-10-26 桌面录制"
            day.mkdir(parents=True)
            _touch(day / "P1.mp4", 1200 * 1000 * 1000, time.time() - 3600)
            _touch(day / "P2.mp4", 1200 * 1000 * 1000, time.time())  # 正在录
            cfg = {"stable_min": 1}
            # P2 刚写入:整组未判稳 -> 不计
            check(fw.unprocessed_bytes(watch, cfg=cfg) == 0,
                  "录制中(有分P未判稳)->目录整组不计入待处理")
            # P2 静置后:整组 2.4GB 计入,超 2GB 阈值
            os.utime(day / "P2.mp4", (time.time() - 3600, time.time() - 3600))
            got = fw.unprocessed_bytes(watch, cfg=cfg)
            check(abs(got - 2.4e9) < 1e6,
                  "判稳后整目录计入(2.4GB,未处理都算)")
            check(got > fw.threshold_bytes(cfg),
                  "录制一天(2.4GB)>2GB 阈值->下一窗口可触发生产")
        finally:
            fw.QUEUE_FILE = real_q2


    print("PASS: " + str(ok) + " 项全部通过")
if __name__ == "__main__":
    main()
