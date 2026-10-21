# -*- coding: utf-8 -*-
"""pet e2e：真实 subprocess 跑 skip-all pipeline（走后台日任务同款入口
folder_watch.run_pipeline_subprocess——UI V1 对齐后前台无手动跑链）。

Popen 打桩注入 --skip 全阶段：秒级完成、零副作用；验证队列路径的
子进程配方（exe/CLI 双形态 argv、UTF-8、退出码与输出收集）。
"""
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, ".")

import toolbox.folder_watch as fw

_real_popen = subprocess.Popen
SKIP = ["--skip", "download", "asr", "speaker", "detect",
        "knockdowns", "pickups", "queues", "voice", "push"]


def _patched_popen(args, **kw):
    if args[1:4] == ["-X", "utf8", "-m"]:
        args = args + SKIP           # 测试注入：全跳过，秒级完成零副作用
    return _real_popen(args, **kw)


fw.subprocess.Popen = _patched_popen

target = Path("video/2026-10-09 桌面录制") / \
    "2026-10-09 桌面录制 P1 09日13点43分.mp4"
assert target.exists(), f"测试素材缺失：{target}"

# 登录门控（REQ-20260919-013）：子进程是独立进程、读真实 config——给它落
# 一个临时本地会话过门（门控只查本地 bearer 不访网，skip-all 不上送），
# 跑完恢复原会话。门控语义本身由 gating_daily_regress 覆盖。
import toolbox.device_identity as di

_real_sess = di.load_session()
di.save_session({"user_id": "e2e", "bearer": "e2e-tok",
                 "expires_at_ts": time.time() + 3600})
try:
    t0 = time.time()
    rc, out = fw.run_pipeline_subprocess(str(target))
    dur = time.time() - t0
finally:
    if _real_sess.get("bearer"):
        di.save_session(_real_sess)
    else:
        di.clear_session()

print("rc =", rc, f"（{dur:.1f}s）")
print("输出尾 =", out[-200:] if out else "")
ok = rc == 0 and "全链完成" in out
print("E2E", "PASS ✅" if ok else "FAIL ❌")
sys.exit(0 if ok else 1)
