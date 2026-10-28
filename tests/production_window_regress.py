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

            # ---- production_due：工作时段规则(2026-10-28,窗口已移除) ----
            import datetime as _dt

            def _at(hour):        # 今天本地时刻 hour 点的 epoch
                return _dt.datetime.now().replace(
                    hour=hour, minute=0, second=0,
                    microsecond=0).timestamp()

            st = fw._load()
            CFG = {"work_start_hour": 4, "work_end_hour": 12}
            due, why = fw.production_due(cfg=CFG, state=st,
                                         now=_at(2), boot=True)
            check(not due and "非工作时段" in why,
                  "02:00 开机在时段外 → 不 due")
            due, _ = fw.production_due(cfg=CFG, state=st,
                                       now=_at(3), boot=False)
            check(not due, "03:00 非开机在时段外 → 不 due")
            due, why = fw.production_due(cfg=CFG, state=st,
                                         now=_at(5), boot=False)
            check(due and "工作时段" in why, "05:00 在时段内 → due")
            due, why = fw.production_due(cfg=CFG, state=st,
                                         now=_at(4), boot=True)
            check(due and "开机补跑" in why, "04:00 边界(含头)开机 → due")
            due, why = fw.production_due(cfg=CFG, state=st,
                                         now=_at(12), boot=False)
            check(not due, "12:00 边界(不含尾) → 不 due")
            due, _ = fw.production_due(cfg=CFG, state=st,
                                       now=_at(23), boot=True)
            check(not due, "23:00 开机在时段外 → 不 due")
            # 跨零点时段(22~6)
            XN = {"work_start_hour": 22, "work_end_hour": 6}
            check(fw.production_due(cfg=XN, state=st, now=_at(23))[0],
                  "跨零点 22~6：23 点在时段内")
            check(not fw.production_due(cfg=XN, state=st, now=_at(12))[0],
                  "跨零点 22~6：12 点在时段外")
            check(fw.production_due(cfg=XN, state=st, now=_at(3))[0],
                  "跨零点 22~6：凌晨 3 点在时段内")
            # 窗口已移除:刚生产完在时段内仍可再触发(不再锁 24h)
            fw.mark_production_run(now=_at(5) - 600)
            due, why = fw.production_due(cfg=CFG, state=fw._load(),
                                         now=_at(5), boot=False)
            check(due, "10 分钟前刚生产,时段内仍 due(滚动窗口已移除)")
            # mark 只作展示记录:ts 落盘但不再阻塞判定
            check(fw.last_production_ts() > 0, "mark 记录仍落盘(展示用)")

            # ---- CPU 采样与退让阈值口径(2026-10-28) ----
            cpu = fw.cpu_percent(sample=0.3)
            check(cpu is None or 0 <= cpu <= 100,
                  f"CPU 采样可用或安全失败(实测 {cpu})")
            import threading as _th
            busy = {"on": False}
            def _spin():
                while busy["on"]:
                    pass
            busy["on"] = True
            _th.Thread(target=_spin, daemon=True).start()
            cpu2 = fw.cpu_percent(sample=0.5)
            busy["on"] = False
            check(cpu2 is None or cpu2 >= cpu if cpu is not None else True,
                  f"满载采样不低于空闲({cpu}% -> {cpu2}%)")
            # 阈值语义:cpu_yield_percent 在 rules 白名单 50~100
            from toolbox import rules_sync as rs
            check(rs._clamp("cpu_yield_percent", 30) == 50.0
                  and rs._clamp("cpu_yield_percent", 95) == 95.0,
                  "cpu_yield 夹紧 50~100(过低误退让被拒)")
            check(rs.parse({"cpu_yield_percent": 99, "work_start_hour": 4,
                            "work_end_hour": 12})
                  == {"cpu_yield_percent": 99.0, "work_start_hour": 4,
                      "work_end_hour": 12},
                  "新键经规则通道可下发")
            mem = fw.mem_percent()
            check(mem is None or 0 <= mem <= 100,
                  f"内存采样可用或安全失败(实测 {mem}%)")
            check(rs._clamp("mem_yield_percent", 30) == 50.0
                  and rs._clamp("mem_yield_percent", 95) == 95.0,
                  "mem_yield 夹紧 50~100")
            check(rs.parse({"mem_yield_percent": 88})
                  == {"mem_yield_percent": 88.0},
                  "mem_yield 经规则通道可下发")

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
