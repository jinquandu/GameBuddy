# -*- coding: utf-8 -*-
"""凌晨生产守夜(运维脚本,2026-10-28)。

背景:用户新放了一个约 20GB 的录像目录,要求"4 点没自动跑就手动跑,
跑失败修好再跑"。本脚本按这个要求做**最多若干轮**的守候:

  第 1 轮 04:05  若应用已在跑 → 直接接管观察,不手动插入
                 若没在跑      → 手动跑一轮识别链
  之后每 20 分钟  若上一轮失败 → 重试(最多 N 轮)
                 若还在跑     → 继续等
                 若已完成     → 退出

设计约束:
- 与应用**共用同一队列与设备目录**(TOOLBOX_DATA_DIR / DSJ_DEVICE_DIR),
  所以不会重复领同一任务;
- 只在"应用确实没启动"时才手动跑(以 last_production_ts 与队列状态判定);
- 每轮结果写 data/logs/pet.log(与后台一致),便于回溯。

用法:
  TOOLBOX_DATA_DIR=.../dist/GameBuddy/data \
  DSJ_DEVICE_DIR=.../dist/GameBuddy/data/.device \
    py -X utf8 tools/watch_production.py --watch "D:/BaiduNetdiskDownload/1028"
"""
import argparse
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from toolbox import folder_watch as fw            # noqa: E402
from toolbox.logs import get as _getlog           # noqa: E402

LOG = _getlog("pet")
START_HOUR = 4
FIRST_CHECK_MIN = 5          # 04:05 首查(给应用 5 分钟启动时间)
RETRY_MIN = 20


def _fmt(ts):
    return time.strftime("%m-%d %H:%M", time.localtime(ts)) if ts else "无"


def state():
    """-> {done_today, running, queued, last_ts, pending_gb}"""
    st = fw._load()
    jobs = st.get("jobs") or {}
    running = [j for j in jobs.values() if j.get("state") == "running"]
    queued = [j for j in jobs.values() if j.get("state") == "queued"]
    last = float(st.get("last_production_ts") or 0)
    today = time.strftime("%Y-%m-%d")
    done_today = (time.strftime("%Y-%m-%d", time.localtime(last)) == today)
    return {"done_today": done_today, "running": len(running),
            "queued": len(queued), "last_ts": last}


def run_once(watch, limit):
    """手动跑一轮:调用 run_produce_once.main(共用同一实现,不复制逻辑)。"""
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "run_produce_once", ROOT / "tools" / "run_produce_once.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.main(["--watch", watch, "--limit", str(limit)]) == 0


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--watch", required=True)
    ap.add_argument("--limit", type=int, default=20)
    ap.add_argument("--rounds", type=int, default=6,
                    help="最多手动重试轮数(默认 6)")
    ap.add_argument("--now", action="store_true",
                    help="不等 04:05,立即执行(用于白天补跑)")
    a = ap.parse_args(argv)

    watch = str(Path(a.watch))
    if not Path(watch).is_dir():
        print(f"[错误] 目录不存在: {watch}")
        return 2

    if not a.now:
        now = time.localtime()
        if now.tm_hour < START_HOUR:
            wait_s = ((START_HOUR - now.tm_hour) * 60
                      + FIRST_CHECK_MIN - now.tm_min) * 60
            print(f"等到 {START_HOUR:02d}:{FIRST_CHECK_MIN:02d} "
                  f"（约 {wait_s / 60:.0f} 分钟）…")
            time.sleep(max(0, wait_s))
        else:
            print(f"已过 {START_HOUR:02d}:05,立即开始守夜")

    fails = 0
    for rnd in range(1, a.rounds + 1):
        s = state()
        pend = fw.unprocessed_bytes(watch)
        print(f"\n=== 第 {rnd} 轮 {time.strftime('%H:%M:%S')} ===")
        print(f"应用状态: 今日已生产={s['done_today']} 处理中={s['running']} "
              f"排队={s['queued']} 上次生产={_fmt(s['last_ts'])}")
        print(f"待处理 {pend / 1e9:.2f}GB / 阈值 "
              f"{fw.threshold_bytes() / 1e9:.0f}GB")
        LOG.info("守夜第%d轮 今日已生产=%s 处理中=%d 排队=%d 待处理%.2fGB",
                 rnd, s["done_today"], s["running"], s["queued"], pend / 1e9)
        if s["running"]:
            print("应用正在跑识别链 → 只观察，不手动插入")
            time.sleep(RETRY_MIN * 60)
            continue
        if s["done_today"] and not s["queued"]:
            print("今日生产已完成且无排队任务 → 结束守夜")
            return 0
        if pend <= fw.threshold_bytes():
            print(f"待处理不足阈值（{pend / 1e9:.2f}GB）→ 不生产（规则_当日权益照算）")
            return 0
        print("应用未启动生产 → 手动跑一轮")
        ok = run_once(watch, a.limit)
        if ok:
            print("手动跑链成功")
            return 0
        fails += 1
        print(f"手动跑链失败（第 {fails} 次）→ 检查错误，{RETRY_MIN} 分钟后重试")
        LOG.warning("守夜第%d轮手动跑链失败", rnd)
        if rnd < a.rounds:
            time.sleep(RETRY_MIN * 60)
    print(f"\n守夜结束：{a.rounds} 轮仍未成功，需人工介入")
    return 1


if __name__ == "__main__":
    sys.exit(main())
