# -*- coding: utf-8 -*-
"""REQ-20261026-001 回归:未处理口径阈值 + 规则服务端下发。

- 口径:未处理目标计入(done+指纹一致才排除;排队/失败/内容更新/从未入队
  都算未处理);写入中不稳定目标不计;根直下散装按根目录聚合(与目录任务
  聚合口径一致,修 scan↔merge 打架)。
- 下发:白名单+夹紧+未知键忽略;候选地址按序;失败沿用缓存;_cfg 合并
  生效(服务端最高);清缓存回内置。
"""
import json
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, ".")

from toolbox import folder_watch as fw
from toolbox import rules_sync as rs

ok = 0


def check(cond, what):
    global ok
    assert cond, "FAIL: " + what
    ok += 1
    print("  ✓ " + what)


# ---------------------------------------------------------------- 规则下发
with tempfile.TemporaryDirectory() as td:
    t = Path(td)
    real_cache = rs.CACHE
    rs.CACHE = t / "client_rules.json"
    real_q = fw.QUEUE_FILE
    fw.QUEUE_FILE = t / "state.json"
    try:
        rs.clear()
        check(rs.applied_overrides() == {}, "下发:初始无缓存=内置默认")

        # 解析:白名单+夹紧+未知键忽略;兼容 {"folder_watch":{...}} 与平铺
        ov = rs.parse({"rules_version": "v1", "folder_watch": {
            "threshold_gb": 1, "scan_interval_min": 60, "daily_hour": 5,
            "stable_min": 5, "production_window_s": 43200,
            "attempts_max": 2, "lease_min": 60}})
        check(ov == {"threshold_gb": 1.0, "scan_interval_min": 60,
                     "daily_hour": 5, "stable_min": 5,
                     "production_window_s": 43200, "attempts_max": 2,
                     "lease_min": 60}, "下发:嵌套形态全键解析")
        ov2 = rs.parse({"threshold_gb": 3, "hack_mode": True})
        check(ov2 == {"threshold_gb": 3.0}, "下发:平铺兼容+未知键忽略")

        ov3 = rs.parse({"threshold_gb": 9999, "scan_interval_min": -5,
                        "daily_hour": "abc", "stable_min": 0})
        check(ov3["threshold_gb"] == 100.0 and ov3["scan_interval_min"] == 10
              and ov3["stable_min"] == 1 and "daily_hour" not in ov3,
              "下发:越界夹紧到边界/类型不符忽略")

        # 落盘+读取
        rs.save({"rules_version": "v-test", "threshold_gb": 1.5})
        check(rs.applied_overrides() == {"threshold_gb": 1.5},
              "下发:缓存读取一致")
        meta = rs.applied_meta()
        check(meta.get("rules_version") == "v-test" and meta.get("applied_at"),
              "下发:meta 带版本与时间")

        # _cfg 合并:服务端覆盖 config 值
        import toolbox.config as tc
        real_load = fw.load_config
        fw.load_config = lambda: {"folder_watch": {"threshold_gb": 2,
                                                   "daily_hour": 4}}
        try:
            c = fw._cfg()
            check(c["threshold_gb"] == 1.5 and c["daily_hour"] == 4,
                  "下发:_cfg 合并(服务端覆盖 threshold,未下发键用本地)")
        finally:
            fw.load_config = real_load

        # poll_once:候选按序,全败静默
        calls = []

        def fake_fetch(url, timeout=10):
            calls.append(url)
            if "95188" in url:
                raise RuntimeError("404")
            return {"rules_version": "v-fb", "threshold_gb": 1}
        real_fetch = rs.fetch
        rs.fetch = fake_fetch
        try:
            ov4 = rs.poll_once()
            check(ov4 == {"threshold_gb": 1.0} and len(calls) >= 2
                  and "8.153.157.239" in calls[-1],
                  "下发:域名失败→IP 兜底成功")
            rs.fetch = lambda url, timeout=10: (_ for _ in ()).throw(
                RuntimeError("net"))
            check(rs.poll_once() is None
                  and rs.applied_overrides() == {"threshold_gb": 1.0},
                  "下发:全失败沿用缓存(不清空)")
        finally:
            rs.fetch = real_fetch

        # 清缓存回内置
        rs.clear()
        check(rs.applied_overrides() == {}, "下发:清缓存回内置默认")

        # ---------------- 未处理口径(同一临时目录继续) ----------------
        wd = t / "watch"
        (wd / "场次A").mkdir(parents=True)
        (wd / "场次B").mkdir(parents=True)
        vid_a = wd / "场次A" / "a.mp4"
        vid_b = wd / "场次B" / "b.mp4"
        vid_c = wd / "c.mp4"          # 根直下散装
        for f, n in ((vid_a, 1000), (vid_b, 2000), (vid_c, 400)):
            f.write_bytes(b"v" * n)
        old = time.time() - 7200
        for f in (vid_a, vid_b, vid_c):
            import os
            os.utime(f, (old, old))       # 稳定(且「老」——新口径不看新旧)
        cfg = {"stable_min": 1}

        check(fw.unprocessed_bytes(wd, cfg=cfg) == 3400,
              "口径:全部未处理(老素材也计入,共 3400)")

        # 场次A 处理完(done+done_key 一致)→ 排除
        fw._save({"jobs": {}})
        fw.scan(wd)
        j_a = next(j for j in fw._load()["jobs"].values()
                   if j["session"] == "场次A")
        fw.finish(j_a["key"], True, "跑链成功")
        check(fw.unprocessed_bytes(wd, cfg=cfg) == 2400,
              "口径:done+指纹一致 → 排除(剩场次B+散装 2400)")

        # 场次A 内容更新(键变化) → 重新计入;scan 重排队
        vid_a.write_bytes(b"v" * (1000 + 500))
        import os
        os.utime(vid_a, (old, old))
        check(fw.unprocessed_bytes(wd, cfg=cfg) == 3900,
              "口径:done 后内容更新 → 重新计入")

        # 失败任务 → 未处理
        vid_a.write_bytes(b"v" * 1000)
        os.utime(vid_a, (old, old))
        fw._save({"jobs": {}})
        fw.scan(wd)
        j_b = next(j for j in fw._load()["jobs"].values()
                   if j["session"] == "场次B")
        j_b["attempts"] = 3
        fw.finish(j_b["key"], False, "三次失败")
        check(fw.unprocessed_bytes(wd, cfg=cfg) == 3400,
              "口径:failed 任务计入未处理")

        # 写入中(不稳定)不计
        vid_d = wd / "场次B" / "growing.mp4"
        vid_d.write_bytes(b"v" * 500)
        os.utime(vid_d, (time.time(), time.time()))    # 刚写,不稳定
        check(fw.unprocessed_bytes(wd, cfg=cfg) == 1400,
              "口径:写入中分片只挡自己那组(场次A 1000+散装 400),不连坐其它组")

        # 根直下散装已被父目录任务聚合 → scan 不再重复入队(修打架)
        fw._save({"jobs": {}})
        n1 = fw.scan(wd)
        st = fw._load()
        root_jobs = [j for j in st["jobs"].values()
                     if j["path"] == str(wd)]
        check(n1 == 2 and len(st["jobs"]) == 2,
              f"口径:两目标入队(场次A+散装;场次B 含写入分片未稳不计)(实得 {n1})")
        fw._merge_dir_tasks(st)                    # 聚合散装到根任务
        fw._save(st)
        n2 = fw.scan(wd)
        check(n2 == 0, "口径:聚合后重扫 0 新(不再循环打架)")
    finally:
        rs.CACHE = real_cache
        fw.QUEUE_FILE = real_q

print("PASS: " + str(ok) + " 项断言全部通过")
