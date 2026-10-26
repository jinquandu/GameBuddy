# -*- coding: utf-8 -*-
"""自动录制全链回归(REQ-20261026-004):假进程探测 + 假录制器,不启动任何
真实程序、不调 ffmpeg。

覆盖:自动录制开关 → 游戏"启动"→ 自动开录 → 游戏"退出"连续 N 拍 →
自动收尾(文件/时长回到未录态)→ 再次启动可重新开录 → 关开关立即停录 →
监控线程退出。Tk 需要真 mainloop(后台线程 after 才派发)。

隔离:队列文件、pet 历史、录像目录全部指向临时目录;不碰真实 data。
"""
import json
import sys
import tempfile
import time
import tkinter as tk
from pathlib import Path

sys.path.insert(0, ".")

import toolbox.pet as petmod
from toolbox import folder_watch as fw
from toolbox import screenrec

FAIL = []
STEPS = []


def check(name, cond, extra=""):
    print(("PASS " if cond else "FAIL ") + name + ((" | " + extra) if extra else ""))
    if not cond:
        FAIL.append(name)


class FakeProbe:
    """可控的"游戏是否在运行"。"""

    running = False
    calls = 0

    def __call__(self, names=None, exclude=None, **kw):
        FakeProbe.calls += 1
        return self.running          # 实例属性(测试里 probe.running=True 是设实例)


class FakeRecorder:
    """假录制器:不碰 ffmpeg,只记录 start/stop 与事件回调。"""

    instances = []

    def __init__(self, root, cfg=None, on_event=None):
        self.root = Path(root)
        self.cfg = dict(cfg or {})
        self.on_event = on_event or (lambda *a: None)
        self.started = self.stopped = self.paused = False
        self.files = [self.root / "2026-10-26 桌面录制" / "P1.mp4"]
        self._cur_part = 1
        FakeRecorder.instances.append(self)

    def start(self):
        self.started = True
        self.on_event("start", {"session": str(self.root),
                                "path": str(self.files[0]),
                                "part": 1, "audio": False})

    def stop(self, timeout=25):
        # 与真录制器一致:收尾完成时发 stop 事件(UI 靠它复位)
        self.stopped = True
        self.on_event("stop", {"secs": self.elapsed(),
                                "files": self.files})
        return self.files

    def elapsed(self):
        return 42.0

    def pause(self):
        self.paused = True

    def resume(self):
        self.paused = False


def main():
    tmp = tempfile.mkdtemp(prefix="autorec_chain_")
    t = Path(tmp)
    watch = t / "watch"
    watch.mkdir()

    # ---- 全部隔离:不碰真实 data/队列/历史
    real_q = fw.QUEUE_FILE
    real_hist = petmod.HIST
    real_rec_cls = screenrec.ScreenRecorder
    real_probe = screenrec.game_running
    fw.QUEUE_FILE = t / "state.json"
    petmod.HIST = t / "history.json"
    screenrec.ScreenRecorder = FakeRecorder
    probe = FakeProbe()
    screenrec.game_running = probe
    petmod.PetWindow._env_auto = lambda self: None          # 不跑 OCR

    win = None
    try:
        win = petmod.PetWindow()
        win.watch_dir = str(watch)
        win.REC_WATCH_S = 0.2            # 加速轮询(生产 10s)
        win.REC_MISS_N = 3
        win.update()

        def phase_start():
            check("初始:自动录制关", win.rec_auto_var.get() is False)
            check("初始:未在录", win._rec is None)
            STEPS.append(("phase", "初始态"))
            win.rec_auto_var.set(True)
            win._rec_auto_toggle()       # 开自动录制 → 起监控线程
            win.after(400, phase_game_on)

        def phase_game_on():
            check("开关已持久化(auto_record=true)",
                  json.loads(Path(petmod.HIST).read_text(encoding="utf-8"))
                  .get("auto_record") is True)
            probe.running = True         # "游戏启动"
            win.after(1200, phase_check_rec)

        def phase_check_rec():
            check("游戏启动 → 自动开录(录制器已 start)",
                  len(FakeRecorder.instances) == 1
                  and FakeRecorder.instances[0].started)
            check("UI:录制中", "录制中" in win.rec_lab.cget("text"),
                  win.rec_lab.cget("text"))
            check("UI:自动录制勾选保持", win.rec_auto_var.get() is True)
            probe.running = False        # "游戏退出"
            win.after(1300, phase_check_stop)

        def phase_check_stop():
            rec = FakeRecorder.instances[0]
            check("游戏退出连续 N 拍 → 自动收尾", rec.stopped)
            check("收尾后回到未录态(_rec=None)", win._rec is None)
            check("UI:回到等待游戏",
                  "等待三角洲行动" in win.rec_lab.cget("text"),
                  win.rec_lab.cget("text"))
            check("UI:提示已录时长/分P数",
                  "已录" in win.rec_note.cget("text")
                  and "分P" in win.rec_note.cget("text"),
                  win.rec_note.cget("text"))
            check("停录后开关恢复可用",
                  str(win.rec_auto_chk.cget("state")) != "disabled")
            probe.running = True         # 再进一次游戏
            win.after(1200, phase_second_rec)

        def phase_second_rec():
            check("再次启动游戏 → 重新开录(第二个实例)",
                  len(FakeRecorder.instances) == 2
                  and FakeRecorder.instances[1].started)
            win.rec_auto_var.set(False)
            win._rec_auto_toggle()       # 关开关:应立即停录
            win.after(1200, phase_off)

        def phase_off():
            check("关开关 → 正在录的立即收尾",
                  FakeRecorder.instances[1].stopped)
            check("关开关 → 监控线程已退出", win._rec_watch_on is False)
            check("关开关 → 状态回「自动录制未开启」",
                  "自动录制未开启" in win.rec_lab.cget("text"),
                  win.rec_lab.cget("text"))
            check("全程无真实 ffmpeg/ffmpeg 录制器被实例化",
                  all(isinstance(r, FakeRecorder)
                      for r in FakeRecorder.instances))
            win.after(300, finish)

        def finish():
            win._on_close()
            win_holder.append(True)
            root_after_quit()

        win_holder = []

        def root_after_quit():
            try:
                win.quit()
            except tk.TclError:
                pass

        def timeout_guard():
            if not win_holder:
                check("未超时完成全链", False, "12s 内未走完")
                try:
                    win.quit()
                except tk.TclError:
                    pass

        win.after(300, phase_start)
        win.after(12000, timeout_guard)
        win.mainloop()
        check("全链在超时前完成", bool(win_holder))
    finally:
        fw.QUEUE_FILE = real_q
        petmod.HIST = real_hist
        screenrec.ScreenRecorder = real_rec_cls
        screenrec.game_running = real_probe
        try:
            if win is not None:
                win.destroy()
        except tk.TclError:
            pass

    if FAIL:
        print("失败 " + str(len(FAIL)) + " 项 ❌：" + str(FAIL))
        sys.exit(1)
    print("PASS: 自动录制全链 13 项断言全部通过（无真实程序启动）")


if __name__ == "__main__":
    main()
