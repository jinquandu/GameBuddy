"""pet 端到端：真实 subprocess 跑 skip-all pipeline，验证窗口走到 done 态。"""
import subprocess
import sys
import time

sys.path.insert(0, ".")

import toolbox.pet as pet

_real_popen = subprocess.Popen
# queues 进 pipeline（2026-10-14）时漏了这里：不在 SKIP 表就会对真场次
# 跑按钮区 OCR（分钟级），60s 超时后测试挂 run 态失败，且旧收尾直接
# destroy() 绕过杀链，每失败一次漏一个孤儿子进程（P0-3 病理现场）
SKIP = ["--skip", "download", "asr", "speaker", "detect",
        "knockdowns", "pickups", "queues", "voice", "push"]


def _patched_popen(args, **kw):
    if args[1:4] == ["-X", "utf8", "-m"]:
        args = args + SKIP           # 测试注入：全跳过，秒级完成零副作用
    return _real_popen(args, **kw)


pet.subprocess.Popen = _patched_popen

win = pet.PetWindow()
win.addr_var.set("video/接直播跑刀 2小时一千万 steam的也可以打 猪猪夏三角洲直播跑刀")
win.run_eval_var.set(False)
win._toggle_run()

t0 = time.time()
while win.state not in ("done", "err", "stop") and time.time() - t0 < 60:
    win.update()
    time.sleep(0.05)

status = win._status_text
done_stages = [n for n, s in win.parser.stages.items() if s["status"] == "skip"]
print("state =", win.state)
print("status =", status)
print("skip 标记数 =", len(done_stages),
      "done_count =", win.parser.done_count())
print("日志尾 =", win._logs[-1] if win._logs else "")
ok = win.state == "done" and "完成" in status and len(done_stages) == 9
if win.proc is None:
    win._on_close()          # 完整清理链（存历史/退托盘/destroy）
else:
    win._stop()              # 兜底杀树：失败路径也不留孤儿子进程
try:
    win.destroy()
except Exception:
    pass
print("E2E", "PASS ✅" if ok else "FAIL ❌")
sys.exit(0 if ok else 1)
