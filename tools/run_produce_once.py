# -*- coding: utf-8 -*-
"""手动跑一轮生产(REQ 运维脚本,2026-10-28)。

用途:应用端后台日任务到点没启动时,人工补跑。行为与后台一致——
扫描入队 → 按队列顺序逐个跑识别链 → 记账(成功 done / 失败重试或 failed),
**不重复劳动**:已 done 且内容未变的目录不会再跑。

与 pet 的关系:本脚本自己领任务(claim),因此应用侧不会重复拉起同一任务;
两者同时在同一目录上工作时,**按队列状态互斥**(谁先 claim 谁跑)。

用法(源码树,必须带配套设备目录,否则推送会 401):
    DSJ_DEVICE_DIR=".../dist/GameBuddy/data/.device" \
        py -X utf8 tools/run_produce_once.py --watch "D:/BaiduNetdiskDownload/1028"

参数:
    --watch   监控目录(必填)
    --limit   最多跑几个任务(默认 20,防误跑整盘)
    --no-eval 关闭评估包上传(默认开启)
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


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--watch", required=True)
    ap.add_argument("--limit", type=int, default=20)
    ap.add_argument("--no-eval", action="store_true")
    ap.add_argument("--check-only", action="store_true",
                    help="只扫描并报告是否有可跑任务，不执行")
    a = ap.parse_args(argv)

    wd = str(Path(a.watch))
    if not Path(wd).is_dir():
        print(f"[错误] 目录不存在: {wd}")
        return 2

    # 队列根提示(重要):本脚本与 pet 各自读写 DATA 下的队列文件。若两边的
    # DATA 根不同,会出现"两套队列各自跑同一目录"的重复劳动。默认让本脚本
    # 复用 pet 的数据根(TOOLBOX_DATA_DIR),否则打印醒目提示。
    from toolbox.config import DATA
    data_root = Path(DATA).resolve()
    dist_data = (ROOT / "dist" / "GameBuddy" / "data")
    print(f"数据根: {data_root}")
    if data_root != dist_data.resolve() and dist_data.is_dir():
        print("  ⚠ 与桌面应用的 data 根不同——若应用正在同一目录上工作,"
              "会各自跑一遍。建议加环境变量:")
        print(f'     TOOLBOX_DATA_DIR="{dist_data.as_posix()}"')
        print(f'     DSJ_DEVICE_DIR="{dist_data.as_posix()}/.device"')

    pending = fw.unprocessed_bytes(wd)
    limit = fw.threshold_bytes()
    print(f"目录 {wd}")
    print(f"待处理 {pending / 1e9:.2f}GB / 阈值 {limit / 1e9:.0f}GB"
          f"（{'达标' if pending > limit else '未达标'}）")

    new = fw.scan(wd)
    print(f"扫描入队: {new} 个新目标")

    jobs = [j for j in fw.jobs_view() if j["state"] == "queued"]
    print(f"队列中可跑 {len(jobs)} 个任务")
    if not jobs:
        print("没有可跑任务（可能已全部处理，或目录尚未判稳）")
        return 0

    if a.check_only:
        print("check-only 模式:不执行")
        return 0

    ok_n = fail_n = 0
    for idx, _ in enumerate(jobs[:a.limit], 1):
        j = fw.claim_next()
        if not j:
            break
        t0 = time.time()
        print(f"\n[{idx}/{min(len(jobs), a.limit)}] 开始: {j['session']}"
              f"（{j['path']}）", flush=True)
        rc, tail = fw.run_pipeline_subprocess(j["path"])
        dur = time.time() - t0
        ok = rc == 0
        note = ("手动跑链成功" if ok else f"退出码{rc}：{tail[-400:]}")
        fw.finish(j["key"], ok, note)
        if ok:
            ok_n += 1
            print(f"  ✓ 完成，用时 {dur / 60:.1f} 分钟")
        else:
            fail_n += 1
            print(f"  ✗ 失败 rc={rc}，用时 {dur / 60:.1f} 分钟")
            print(f"  尾部输出:\n{tail[-800:]}")
            LOG.warning("手动跑链失败 %s（rc=%s）", j["session"], rc)
        LOG.info("手动跑链结束 %s ok=%s %.1f分钟", j["session"], ok, dur / 60)

    print(f"\n本轮完成：成功 {ok_n} / 失败 {fail_n}")
    return 0 if fail_n == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
