"""pet.py 自测：解析器状态机 + latest_stats + GUI 冒烟（无人工交互）。"""
import sys
import tkinter as tk

sys.path.insert(0, ".")

from toolbox.pet import PetWindow, ProgressParser, latest_stats

FAIL = []


def check(name, cond):
    print(("PASS " if cond else "FAIL ") + name)
    if not cond:
        FAIL.append(name)


# ---- 1) 解析器：喂一遍仿真 pipeline 日志 ----
p = ProgressParser()
LOG = [
    "[pipeline] download 开始 ...",
    "  合并完成: 猪猪夏 P1 18日12点52分.mp4 (1331MB)",
    "[pipeline] download 完成 (95s)",
    "[pipeline] 场次：猪猪夏三角洲直播跑刀，3 个分片",
    "ASR 转写：3 个分片（模型加载一次，逐分片转写）",
    "[pipeline] asr 完成 (312s)",
    "[pipeline] -- 跳过 speaker",
    "  P1：复用事件流 xxx_full（61 条）",
    "[pipeline] detect 完成 (5s)",
    "[pipeline] knockdowns 开始 ...",
    "共 0 个击倒片段 + knockdowns.json 已写入 data/knockdowns/xxx",
    "[pipeline] knockdowns 完成 (12s)",
    "  P3：7 粗簇 -> 14 会话",
    "共 34 个拾取会话片段 + pickups.json 已写入 data/pickups/xxx",
    "[pipeline] pickups 完成 (40s)",
    "review 页已生成（1 场次 / 31 片段）: data/voice/review.html",
    "[pipeline] voice 完成 (25s)",
    "评估包：7 组，约 1.21GB",
    "[pipeline] push 完成 (180s)",
    "[pipeline] 全链完成。download=95s asr=312s",
]
for line in LOG:
    p.update(line)

check("download=done/95s", p.stages["download"] == {"status": "done", "secs": 95})
check("speaker=skip", p.stages["speaker"]["status"] == "skip")
check("asr=done", p.stages["asr"]["status"] == "done")
check("voice=done", p.stages["voice"]["status"] == "done")
check("场次解析", p.session == "猪猪夏三角洲直播跑刀" and p.parts == 3)
check("击倒计数=0", p.counts["knockdowns"] == 0)
check("拾取计数=34", p.counts["pickups"] == 34)
check("完成标记", p.finished)
check("细目行=评估包", "评估包" in p.detail)

# 错误行
p2 = ProgressParser()
p2.update("\n[错误] 需要 GNU rsync")
check("错误行捕获", p2.error_line and "rsync" in p2.error_line)

# ---- 2) latest_stats：对着现有 data/ 树 ----
st = latest_stats()
print("latest_stats ->", st)
check("最近场次是猪猪夏", "猪猪夏" in st["session"])
check("击倒=0", st["knockdowns"] == 0)
check("拾取=34", st["pickups"] == 34)
check("语音=31", st["voice"] == 31)

# ---- 3) GUI 冒烟：创建 -> 两个动画帧 -> 收起 -> 展开 -> 销毁 ----
try:
    win = PetWindow()
    check("窗口创建", True)
    win.update()                       # 渲染一帧
    win._animate()                     # 手动触发动画回调一次
    win.update()
    bubble0 = win.pet_cv.itemcget(win.bubble, "text")
    check("气泡文案", bool(bubble0))
    check("阶段表 8 项", len(win.stage_labels) == 8)
    win._toggle_collapse()
    win.update()
    check("收起", win.collapsed and win.winfo_height() <= 130)
    win._toggle_collapse()
    win.update()
    check("展开", not win.collapsed and win.winfo_height() >= 400)
    # 地址校验分支（不真正起子进程）
    win.addr_var.set("")
    win._toggle_run()
    win.update()
    check("空地址提示", "地址" in win.pet_cv.itemcget(win.bubble, "text"))
    win._save_hist()
    win.destroy()
    check("GUI 冒烟完成", True)
except tk.TclError as e:
    check(f"GUI 冒烟（TclError: {e}）", False)

print("\n" + ("全部通过 ✅" if not FAIL else f"失败 {len(FAIL)} 项 ❌：{FAIL}"))
sys.exit(1 if FAIL else 0)
