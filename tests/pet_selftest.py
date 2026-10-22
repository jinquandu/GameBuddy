# -*- coding: utf-8 -*-
"""pet.py 自测：解析器状态机 + latest_stats + 主窗 GUI 冒烟（无人工交互）。

主窗 = UI V1 评审稿对齐版（REQ-20261021-003 范围2）：左导航工作台/我的
作品/账号设置 + 卡片；前台无手动跑链/BV/URL/监控行。冒烟覆盖：导航
切换、生产徽标状态机（登录/绑定/权益/窗口各分支纯渲染）、目录设置、
flash、托盘（装了 pystray 才验）。
"""
import sys
import tkinter as tk

sys.path.insert(0, ".")

from toolbox.pet import PetWindow, ProgressParser, latest_stats, DATA

FAIL = []


def check(name, cond):
    print(("PASS " if cond else "FAIL ") + name)
    if not cond:
        FAIL.append(name)


# ---------------- 1) ProgressParser 状态机（与旧版同源保留，CLI 消费）
p = ProgressParser()
p.update("[pipeline] 场次：测试场次，2 个分片")
p.update("[pipeline] download 开始")
p.update("[pipeline] download 完成 (12s)")
p.update("[pipeline] -- 跳过 asr")
check("解析：跳过=done 口径", p.done_count() == 2)
frac, cur = p.progress()
check("解析：进度与当前阶段", 0 < frac < 1 and cur is None)
p.update("[pipeline] 全链完成。")
check("解析：完成标志", p.progress()[0] == 1.0)

check("latest_stats 形状", set(latest_stats()) == {
    "session", "knockdowns", "pickups", "voice", "queues"})

# ---------------- 2) 主窗 GUI 冒烟（UI V1 对齐版）
# 本机可能已登录(真实会话):屏蔽「登录后自动环境检测」(会真跑 OCR 基准 ≈10s),
# 检测徽标由下面的纯渲染断言覆盖
import toolbox.pet as _petmod
_real_env_auto = _petmod.PetWindow._env_auto
_petmod.PetWindow._env_auto = lambda self: None
try:
    win = PetWindow()
    win.update()
except Exception:
    _petmod.PetWindow._env_auto = _real_env_auto
    raise

check("左导航三页齐", set(win._pages) == {"home", "works", "settings"}
      and all(win._nav_btns[k].winfo_ismapped() for k in win._nav_btns))
check("新手教程入口已移除（REQ-20261022-005 替换为环境检测）",
      not hasattr(win, "tutorial_btn") and not hasattr(win, "_tutorial_start"))
check("开始剪辑条件四项齐（登录/权益/微信/环境）",
      set(win.pre_labs) == {"login", "member", "bind", "env"})
win._nav("settings")
win.update()
check("导航切换：设置页可见",
      win._pages["settings"].winfo_ismapped()
      and not win._pages["home"].winfo_ismapped())
win._nav("home")
win.update()

# 生产徽标状态机（纯渲染分支，不起线程不发网；persist 打桩防写真 config）
import tempfile
from pathlib import Path

import toolbox.config as _tc
_real_persist = _tc.persist_watch_dir
_tc.persist_watch_dir = lambda d: None
try:
    win._render_pre({"logged_in": True, "member": True,
                     "member_expires": None, "binding": "none", "opt_in": None})
    win._render_prod({"wdir": "D:/录像",
                      "summary": {"queued": 2, "running": 0, "done": 5,
                                  "failed": []},
                      "last_ts": 0, "due": (True, "到达生产时刻"),
                      "added": 1e9, "limit": 2e9})
    win.update()
    check("徽标：未绑定分支", "微信还没绑定" in win.prod_badge.cget("text")
          and win.prod_act.winfo_ismapped())
    check("前置：未绑定亮去绑定", "微信✗" in win.pre_labs["bind"].cget("text")
          and win.bind_btn.winfo_manager() == "pack")
    check("侧栏权益卡：有效", "权益 ✓" in win.side_member.cget("text"))

    # 环境检测徽标（REQ-20261022-005）：未登录=·；有结果后按通过/失败着色
    win._sess = {}
    win._env = {}
    win._render_env()
    win.update()
    check("环境徽标：未登录为未检测",
          "环境·" in win.pre_labs["env"].cget("text")
          and "登录后自动检测" in win.env_detail.cget("text")
          and str(win.env_btn.cget("state")) == "disabled")
    win._sess = {"user_id": "u1"}
    win._env = {"ok": True, "warn": 0, "checked_at": "2026-10-22 10:00:00",
                "items": [], "estimate": {"min_per_30": 3.5}}
    win._render_env()
    win.update()
    check("环境徽标：通过 ✓ 且给出预计耗时",
          "环境✓" in win.pre_labs["env"].cget("text")
          and "3.5 分钟" in win.env_detail.cget("text")
          and str(win.env_btn.cget("state")) == "normal")
    win._env = {"ok": False, "bad": 1, "warn": 0,
                "checked_at": "2026-10-22 10:00:00",
                "items": [{"key": "ocr", "label": "画面识别",
                           "level": "bad", "detail": "太慢"}]}
    win._render_env()
    win.update()
    check("环境徽标：失败 ✗ 且点名问题项",
          "环境✗" in win.pre_labs["env"].cget("text")
          and "画面识别" in win.env_detail.cget("text"))
    # 共用电脑换号:上一个账号的检测结果不算到新账号头上
    win._sess = {"user_id": "u2"}
    win._env = {"user_id": "u1", "ok": True, "warn": 0,
                "checked_at": "2026-10-22 10:00:00", "items": []}
    win._render_env()
    win.update()
    check("环境徽标：换账号后按未检测显示",
          "环境·" in win.pre_labs["env"].cget("text")
          and "本账号还没检测" in win.env_detail.cget("text"))
    win._sess = {}
    win._env = {}

    win._render_pre({"logged_in": True, "member": True,
                     "member_expires": None, "binding": "bound",
                     "opt_in": True})
    win._render_prod({"wdir": "D:/录像",
                      "summary": {"queued": 0, "running": 0, "done": 5,
                                  "failed": []},
                      "last_ts": 1234567890,
                      "due": (False, "窗口内已生产（23h 前）"),
                      "added": 3e9, "limit": 2e9})
    win.update()
    # REQ-20261022-008:只有本机时间戳、无云端确认时不得报「已出片」
    check("徽标：无云端确认 → 结果待确认(不虚报已出片)",
          "结果待确认" in win.prod_badge.cget("text")
          and "已出片" not in win.prod_badge.cget("text"))
    check("通知开关：已绑定可用", str(win.opt_in_chk.cget("state"))
          != "disabled" and win.opt_in_var.get() is True)

    # 云端 produce=done + 最近作品页 → 才显示已出片,作品页按钮可用
    import time as _t
    win._produce = {"场次X": {"state": "done", "ts": _t.time(),
                              "works_url": "/eval/usr_x/preview/x"}}
    win._last_works = {"session": "场次X", "works_url": "/eval/usr_x/preview/x",
                       "ts": _t.time()}
    win._render_prod({"wdir": "D:/录像",
                      "summary": {"queued": 0, "running": 0, "done": 1,
                                  "failed": []},
                      "last_ts": 1234567890, "due": (False, "窗口内已生产"),
                      "added": 3e9, "limit": 2e9,
                      "produce": dict(win._produce),
                      "last_works": dict(win._last_works)})
    win.update()
    check("徽标：云端确认后才显示已出片",
          "已出片" in win.prod_badge.cget("text"))
    check("作品页：真实成片时按钮可用",
          str(win.works_btn.cget("state")) == "normal")

    # 队列等待原因与进度(REQ-008 C02)
    win._render_prod({"wdir": "D:/录像",
                      "summary": {"queued": 1, "running": 1, "done": 0,
                                  "failed": []},
                      "progress": {"session": "场次Y", "stage": "画面识别",
                                   "index": 4, "total": 9},
                      "jobs": [{"key": "k1", "session": "场次Y",
                                "state": "running", "note": "",
                                "path": "D:/录像/场次Y"},
                               {"key": "k2", "session": "场次Z",
                                "state": "failed",
                                "note": "退出码1：识别失败",
                                "path": "D:/录像/场次Z"},
                               {"key": "k3", "session": "场次W",
                                "state": "queued", "note": "",
                                "path": "D:/录像/场次W"}],
                      "produce": {}, "last_works": {},
                      "next_scan_ts": _t.time() + 3600})
    win.update()
    check("队列：处理中显示阶段", "画面识别" in win.prod_detail.cget("text"))
    rows = [w for w in win.queue_rows.winfo_children()]
    texts = []
    for r in rows:
        for c in r.winfo_children():
            if hasattr(c, "cget"):
                try:
                    texts.append(str(c.cget("text")))
                except Exception:
                    pass
            for cc in getattr(c, "winfo_children", lambda: [])():
                try:
                    texts.append(str(cc.cget("text")))
                except Exception:
                    pass
    joined = " | ".join(texts)
    check("队列：失败显示原因", "识别失败" in joined)
    check("队列：排队显示等待原因", ("下次自动检查" in joined or "等待" in joined))
    win._produce = {}
    win._last_works = {}

    win._render_prod({"wdir": "", "summary": {"queued": 0, "running": 0,
                                              "done": 0, "failed": []}})
    win.update()
    check("徽标：未选目录", "选择录像文件夹" in win.prod_badge.cget("text"))
    # 未配置目录(隔离真实 config,避免本机已有设置干扰)
    import toolbox.pet as _pm
    _real_lc = _pm.load_config
    _pm.load_config = lambda: {"folder_watch": {}}
    try:
        win.watch_dir = ""
        win._render_dir()
        win.update()
        check("目录：未配置时不显示 None",
              "None" not in win.dir_lab.cget("text")
              and "未选择" in win.dir_lab.cget("text"))
    finally:
        _pm.load_config = _real_lc

    # 目录设置（persist 已打桩，不写真 config）
    with tempfile.TemporaryDirectory() as td:
        win._set_watch_dir(td)
        win.update()
        check("目录渲染两处同步", td[:20] in win.dir_lab.cget("text")
              and "录像目录" in win.set_dir_lab.cget("text"))
finally:
    _tc.persist_watch_dir = _real_persist
    _petmod.PetWindow._env_auto = _real_env_auto
    win._save_hist()

# 状态行短闪
win._flash("测试提示")
win.update()
check("flash 生效", "测试提示" in win._status_text)

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
check("托盘驻留（装 pystray 时）", (not has_tray) or win.tray is not None)
win._tray_show()
win.update()
check("托盘恢复主窗", win.wm_state() == "normal")

win._on_close()
try:                       # destroy 后 winfo_exists 本身会 TclError
    gone = not win.winfo_exists()
except tk.TclError:
    gone = True
check("收尾销毁", gone)

if FAIL:
    print("失败 " + str(len(FAIL)) + " 项 ❌：" + str(FAIL))
    sys.exit(1)
print("全部通过 ✅")
