"""pet 端到端：真实 subprocess 跑 skip-all pipeline，验证窗口走到 done 态。"""
import subprocess
import sys
import time

sys.path.insert(0, ".")

import toolbox.pet as pet

_real_popen = subprocess.Popen
SKIP = ["--skip", "download", "asr", "speaker", "detect",
        "knockdowns", "pickups", "voice", "push"]


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
ok = win.state == "done" and "完成" in status and len(done_stages) == 8
win._on_close() if win.proc is None else None
win.destroy()
print("E2E", "PASS ✅" if ok else "FAIL ❌")
sys.exit(0 if ok else 1)
