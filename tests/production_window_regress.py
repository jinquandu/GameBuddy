# -*- coding: utf-8 -*-
"""扫描/生产分离·24h 滚动窗口回归（REQ-20261021-001 规则 2-4）。

覆盖（folder_watch 无 UI 依赖，纯逻辑）：
- new_recordings_bytes：稳定文件 mtime 终值口径的近 24h 新增字节——窗口内
  新文件计、历史素材（旧 mtime）不计、写入中不计（含组内一文件在写则
  整组未完成）、窗口下界开区间、散装/子目录两种形态；
- production_due：开机补跑（boot 无小时门）/≥daily_hour 双触发 × 滚动
  24h 窗口（窗口内不重复触发）；
- mark_production_run/last_production_ts 持久化往返；
- threshold_bytes 十进制 GB 换算（严格 > 语义由调用侧 added > limit 表达）。
"""
import os
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, ".")

from toolbox import folder_watch as fw

H = 3600.0


def _touch(path, size, mtime):
    path.write_bytes(b"x" * size)
    os.utime(path, (mtime, mtime))


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
        try:
            now = time.time()
            cfg = {"stable_min": 0}          # 判稳即时（mtime 旧即稳）

            # ---- new_recordings_bytes：口径四则 ----
            w1 = t / "watch1"
            w1.mkdir()
            _touch(w1 / "old.mp4", 1000, now - 30 * H)   # 历史素材
            _touch(w1 / "new.mp4", 2000, now - 2 * H)    # 近 24h 新增
            _touch(w1 / "loose_old.flv", 4000, now - 40 * H)
            check(fw.new_recordings_bytes(w1, now=now, cfg=cfg) == 2000,
                  "新增字节：窗口内新文件计，历史素材/旧散装不计")
            # 写入中：mtime 刚刚更新 → 未稳定 → 不计（1 分钟判稳）
            cfg_stable = {"stable_min": 1}
            _touch(w1 / "writing.mp4", 8000, now - 2)
            check(fw.new_recordings_bytes(w1, now=now, cfg=cfg_stable)
                  == 2000, "写入中文件不计（mtime 未静置）")
            (w1 / "writing.mp4").unlink()

            # 子目录组：组内一文件在写 → 整组未完成，新文件也不计
            w2 = t / "watch2"
            g = w2 / "场次A"
            g.mkdir(parents=True)
            _touch(g / "P1.mp4", 500, now - 3 * H)
            _touch(g / "P2.mp4", 600, now - 3 * H)
            check(fw.new_recordings_bytes(w2, now=now, cfg=cfg) == 1100,
                  "子目录组：稳定组内窗口内文件累计")
            _touch(g / "P3.mp4", 700, now - 1)
            check(fw.new_recordings_bytes(w2, now=now, cfg=cfg_stable) == 0,
                  "组内一文件在写 → 整组未完成不计")
            os.utime(g / "P3.mp4", (now - 3 * H, now - 3 * H))
            # 组内历史文件不占窗口，但也不阻断稳定组（P1+P2+P3=1800 在窗内）
            _touch(g / "P0_历史.mp4", 900, now - 50 * H)
            check(fw.new_recordings_bytes(w2, now=now, cfg=cfg) == 1800,
                  "组内历史素材不计入（不复用旧素材）")

            # 窗口下界开区间：mtime == now-24h 恰在界外
            _touch(w1 / "edge.mp4", 300, now - 24 * H)
            got = fw.new_recordings_bytes(w1, now=now, cfg=cfg)
            check(got == 2000, "窗口下界开区间（恰好 24h 前=界外）")
            (w1 / "edge.mp4").unlink()

            # 非existent 目录
            check(fw.new_recordings_bytes(t / "nope", now=now, cfg=cfg) == 0,
                  "目录不存在 → 0")

            # ---- threshold_bytes：十进制 GB ----
            check(fw.threshold_bytes({}) == 2e9,
                  "阈值缺省 2 十进制 GB = 2e9 字节")
            check(fw.threshold_bytes({"threshold_gb": 0.5}) == 5e8,
                  "阈值可配（0.5GB=5e8）")

            # ---- production_due：双触发 × 滚动窗口 ----
            import datetime as _dt

            def _at(hour):        # 今天本地时刻 hour 点的 epoch
                return _dt.datetime.now().replace(
                    hour=hour, minute=0, second=0,
                    microsecond=0).timestamp()

            st = fw._load()
            due, why = fw.production_due(
                cfg={"daily_hour": 4}, state=st, now=_at(2), boot=True)
            check(due and "开机补跑" in why,
                  "开机补跑：02:00 无小时门，从未生产 → due")
            due, _ = fw.production_due(
                cfg={"daily_hour": 4}, state=st, now=_at(3), boot=False)
            check(not due, "非开机：03:00 < 04:00 → 不 due")
            due, why = fw.production_due(
                cfg={"daily_hour": 4}, state=st, now=_at(5), boot=False)
            check(due and "生产时刻" in why, "非开机：05:00 ≥ 04:00 → due")

            # 窗口内已生产 → 不 due（开机/到点都不再触发；锚定判定时刻）
            fw.mark_production_run(now=_at(5) - 23 * H)
            due, why = fw.production_due(
                cfg={"daily_hour": 4}, state=fw._load(),
                now=_at(5), boot=True)
            check(not due and "窗口内已生产" in why,
                  "判定前 23h 已生产 → 窗口内不重复触发（含开机）")
            fw.mark_production_run(now=_at(5) - 25 * H)
            due, _ = fw.production_due(
                cfg={"daily_hour": 4}, state=fw._load(),
                now=_at(5), boot=False)
            check(due, "判定前 25h 已生产 → 窗口过期再 due")

            # ---- 持久化往返 ----
            ts = now - 1234
            fw.mark_production_run(now=ts)
            check(fw.last_production_ts() == ts,
                  "last_production_ts 持久化往返")
            fw.mark_production_run(now=now)
            check(fw.last_production_ts() == now, "重复标记覆盖取新")

            # 阈值严格 >：恰好等于不触发（调用侧表达式口径锁定）
            check(not (2e9 > fw.threshold_bytes({})),
                  "恰好 2GB → 严格 > 不成立（不触发）")
        finally:
            fw.QUEUE_FILE = real_q
    print("PASS: " + str(ok) + " 项断言全部通过")


if __name__ == "__main__":
    main()
