"""pet.py 自测：解析器状态机 + latest_stats + GUI 冒烟（无人工交互）。"""
import sys
import tkinter as tk

sys.path.insert(0, ".")

from toolbox.pet import PetWindow, ProgressParser, latest_stats, DATA

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

# ---- 2) latest_stats：对着现有 data/ 树（结构性校验，不硬编码本地数据）----
st = latest_stats()
print("latest_stats ->", st)
check("键完整", set(st) == {"session", "knockdowns", "pickups", "voice",
                            "queues"})
check("计数类型", all(st[k] is None or (isinstance(st[k], int) and st[k] >= 0)
                      for k in ("knockdowns", "pickups", "voice", "queues")))
vdirs = [p.name for p in (DATA / "voice").iterdir()] \
    if (DATA / "voice").is_dir() else []
check("场次名与产物目录对得上",
      not st["session"]
      or (DATA / "knockdowns" / st["session"]).is_dir()
      or any(d.startswith(st["session"]) for d in vdirs))

# ---- 3) GUI 冒烟：素朴小窗创建 -> 渲染 -> 空地址提示 -> 托盘往返 -> 销毁 ----
try:
    win = PetWindow()
    check("窗口创建", True)
    win.update()
    win._render()                      # 手动渲染一帧（状态行刷新）
    win.update()
    check("素朴主界面：目录框+主按钮映射中",
          bool(win.addr_ent.winfo_ismapped()) and bool(win.btn.winfo_ismapped()))
    check("主按钮文案", "开始跑链" in win.btn.cget("text"))
    # 地址校验分支（不真正起子进程）
    win.addr_var.set("")
    win._toggle_run()
    win.update()
    check("空地址提示", "目录" in win._status_text)
    # BV/URL 拒入（IDEA-003 目录唯一入口）：异常简短提示、不起子进程
    win.addr_var.set("BV1xx411c7mD")
    win._toggle_run()
    win.update()
    check("BV/URL 拒入提示", "仅支持本地录像目录" in win._status_text
          and (win.proc is None))
    # 最小化 -> 托盘（withdraw，任务栏不占位）-> 恢复
    win.iconify()
    win.update()
    win._to_tray()
    win.update()
    try:
        import pystray
        has_tray = True
    except ImportError:
        has_tray = False
    check("最小化藏进托盘", win.wm_state() == "withdrawn"
          and (win.tray is not None) == has_tray)
    win._tray_show()
    win.update()
    check("托盘恢复窗口", win.wm_state() == "normal")
    # 点 X 必须走 _on_close 清理链（P0-3：默认销毁会让跑链子进程成孤儿）；
    # 协议事件无法 event_generate，注册用查询式验证，清理链直接调用走通
    reg = win.protocol("WM_DELETE_WINDOW")     # 无参调用 = 查询已注册处理器
    print("   WM_DELETE_WINDOW ->", reg)
    check("点 X 注册到清理链", bool(reg) and "_on_close" in reg)
    win._on_close()
    try:
        win.update()
    except tk.TclError:
        pass    # 销毁后残余事件，忽略
    try:         # PetWindow 即根窗口，destroy 后 winfo 也一并没了
        alive = bool(win.winfo_exists())
    except tk.TclError:
        alive = False
    check("清理链销毁窗口", not alive)
    check("GUI 冒烟完成", True)
except tk.TclError as e:
    check(f"GUI 冒烟（TclError: {e}）", False)

print("\n" + ("全部通过 ✅" if not FAIL else f"失败 {len(FAIL)} 项 ❌：{FAIL}"))
sys.exit(1 if FAIL else 0)
