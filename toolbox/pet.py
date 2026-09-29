"""桌面宠物客户端（大师姐版·紧凑卡）：置顶小窗呈现识别链进展，配置录像地址一键开跑。

python -m toolbox pet [--address <BV号/URL/场次目录>]

设计见 docs/pet-design/DESIGN.md（2026-09-22 依据人设图设计；v1.1 按用户
反馈移除立绘区与头像，改为紧凑工具卡 + 状态行报告进展）：
- 结构：标题栏（名称+租户徽章，拖动区）/ 状态行（彩色一句话报进展）/
  战绩条 / 8 阶段时间线（悬停看日志尾行）/ 配置卡（录像地址+租户ID+推评估）/
  珊瑚粉渐变主按钮；圆角窗口用 -transparentcolor 魔法色角贴片，失败降级直角。
- 状态机：idle 待命 / run 跑链（状态行显示 n/8 与当前阶段）/ done 完成 /
  err 出错 / stop 停止。
- 租户 ID：读 config.yaml server.tenant，界面可改并回写（跑链中禁改）。
- 收起态：标题栏+状态行的小条，点「—」恢复展开。

调试：设 PET_SNAPSHOT=<目录> 启动后自动对各状态截图存 PNG（UI 回归用）。
"""
import json
import os
import queue
import re
import subprocess
import sys
import threading
import time
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

from toolbox.config import DATA, ROOT, load_config
from toolbox.logs import get as _get_log

LOG = _get_log("pet")
LOG_PIPE = _get_log("pipeline")       # 跑链子进程全量输出（排障用）

# ---------------------------------------------------------------- 常量

STAGES = [  # (阶段名, 显示名) —— 与 pipeline.run_pipeline 的 stage 顺序一致
    ("download", "下载"), ("asr", "ASR 转写"), ("speaker", "说话人区分"),
    ("detect", "OCR 行为检测"), ("knockdowns", "击倒切片"), ("pickups", "拾取切片"),
    ("voice", "语音情绪评估"), ("push", "推评估包"),
]
GLYPH = {"pending": ("○", "dim"), "run": ("▶", "accent"),
         "done": ("✓", "good"), "skip": ("—", "dim"), "err": ("✗", "bad")}
STATE_COLOR = {"idle": "lilac", "run": "accent", "done": "good",
               "err": "bad", "stop": "warn"}

# 人设配色（DESIGN.md §2）
C = {"bg": "#FFFEFC", "cream": "#FDF7F2", "milk": "#F7F5F2", "line": "#EEE3DA",
     "hdr": "#FFF6EF", "fg": "#3A3238", "dim": "#8C7F88", "lilac": "#9B6B8F",
     "accent": "#E8663C", "coral": "#F08A85",
     "gold": "#E8A93C", "good": "#4CA37E", "warn": "#D99A3C", "bad": "#D9534F",
     "magic": "#FF00FF"}
FONT = "Microsoft YaHei UI"
W = 342                        # 卡片宽（收起仅减高度，宽度不变）
ASSETS = Path(__file__).resolve().parent / "pet_assets"
HIST = DATA / "pet" / "history.json"
CFG_YAML = ROOT / "config.yaml"


def _or_dash(v):
    return "-" if v is None else v


def load_tenant():
    try:
        return str((load_config().get("server") or {}).get("tenant") or "default")
    except Exception:
        return "default"


def save_tenant(value):
    """回写 config.yaml 的 server.tenant（文本级替换，保留注释与其余键）。

    config.yaml 不存在时新建最小块（load_config 会与 DEFAULTS 深合并）。
    """
    lines = CFG_YAML.read_text(encoding="utf-8").splitlines() \
        if CFG_YAML.exists() else []
    out, in_server, done = [], False, False
    for ln in lines:
        m = re.match(r"^(\s*)tenant:\s*(\S+.*)$", ln)
        if m and in_server:
            out.append(f'{m.group(1)}tenant: "{value}"   # 桌宠界面修改')
            done = True
            continue
        if re.match(r"^server:\s*$", ln):
            in_server = True
        elif ln and not ln[0].isspace():
            in_server = False
        out.append(ln)
    if not done:
        if any(re.match(r"^server:\s*$", l) for l in out):
            i = next(i for i, l in enumerate(out)
                     if re.match(r"^server:\s*$", l))
            out.insert(i + 1, f"  tenant: {value}")
        else:
            out += ["", "server:", f"  tenant: {value}"]
    CFG_YAML.write_text("\n".join(out) + "\n", encoding="utf-8")


# ---------------------------------------------------------------- 日志解析

class ProgressParser:
    """把 pipeline 子进程 stdout 行解析成 UI 状态；喂全量行即可。

    解析目标（与 pipeline.py / 各链路 print 格式一一对应）：
      [pipeline] {name} 开始 ... / {name} 完成 ({n}s) / -- 跳过 {name}
      [pipeline] 场次：{session}，{n} 个分片 / 全链完成。
      共 {n} 个击倒片段 / 共 {n} 个拾取会话片段
    其余信息行择要进状态行（完成/复用/粗簇/触发服务端等）。
    """

    _DETAIL_KW = re.compile(
        r"完成|跳过|复用|重连|重试|粗簇|扫描|转写|合并|触发|评估包|已写入"
        r"|已存在|分片|簇|条）|会话")
    _RE_SKIP = re.compile(r"^-- 跳过 (\w+)$")
    _RE_START = re.compile(r"^(\w+) 开始")
    _RE_DONE = re.compile(r"^(\w+) 完成 \((\d+)s\)$")
    _RE_SESSION = re.compile(r"^场次：(.+?)，(\d+) 个分片$")
    _RE_KD = re.compile(r"共 (\d+) 个击倒片段")
    _RE_PK = re.compile(r"共 (\d+) 个拾取会话片段")

    def __init__(self):
        self.stages = {name: {"status": "pending", "secs": None}
                       for name, _ in STAGES}
        self.session = ""
        self.parts = 0
        self.counts = {"knockdowns": None, "pickups": None}
        self.detail = ""
        self.finished = False
        self.error_line = None

    def update(self, line):
        s = line.strip()
        m = re.match(r"\[pipeline\]\s*(.*)", s)
        if m:
            body = m.group(1)
            mm = self._RE_SKIP.match(body)
            if mm and mm.group(1) in self.stages:
                self.stages[mm.group(1)]["status"] = "skip"
                return
            mm = self._RE_START.match(body)
            if mm and mm.group(1) in self.stages:
                self.stages[mm.group(1)]["status"] = "run"
                return
            mm = self._RE_DONE.match(body)
            if mm and mm.group(1) in self.stages:
                self.stages[mm.group(1)].update(status="done",
                                                secs=int(mm.group(2)))
                return
            mm = self._RE_SESSION.match(body)
            if mm:
                self.session, self.parts = mm.group(1), int(mm.group(2))
                if self.stages["download"]["status"] == "pending":
                    self.stages["download"]["status"] = "skip"  # 本地目录无下载
                return
            if body.startswith("全链完成"):
                self.finished = True
            return
        if "[错误]" in s:
            self.error_line = s[:80]
            return
        mm = self._RE_KD.search(s)
        if mm:
            self.counts["knockdowns"] = int(mm.group(1))
            self.detail = s
            return
        mm = self._RE_PK.search(s)
        if mm:
            self.counts["pickups"] = int(mm.group(1))
            self.detail = s
            return
        if len(s) <= 90 and self._DETAIL_KW.search(s):
            self.detail = s

    def running_stage(self):
        for name, disp in STAGES:
            if self.stages[name]["status"] == "run":
                return disp
        return ""

    def done_count(self):
        return sum(1 for s in self.stages.values()
                   if s["status"] in ("done", "skip"))


# ---------------------------------------------------------------- 本地三线统计

def _clips_len(p: Path):
    try:
        return len(json.loads(p.read_text(encoding="utf-8")).get("clips", []))
    except (OSError, ValueError):
        return None


def latest_stats():
    """最近一场的三线计数：击倒/拾取取 mtime 最新的 JSON；语音按场次前缀分组合计。

    刚跑完的场次产物必然最新，空闲态展示它即「目前的进展」。
    """
    out = {"session": "", "knockdowns": None, "pickups": None, "voice": None}

    def newest(sub, idx):
        root = DATA / sub
        if not root.is_dir():
            return None, None
        cands = [(f.stat().st_mtime, f.parent.name, f)
                 for f in root.glob(f"*/{idx}") if f.is_file()]
        if not cands:
            return None, None
        _, name, f = max(cands)
        return name, _clips_len(f)

    out["session"], out["knockdowns"] = newest("knockdowns", "knockdowns.json")
    _, out["pickups"] = newest("pickups", "pickups.json")

    vroot = DATA / "voice"                        # 语音目录按 <场次>_P<k> 前缀分组
    groups = {}
    if vroot.is_dir():
        for d in vroot.iterdir():
            m = re.match(r"^(.*)_P\d", d.name)
            f = d / "voice_scores.json"
            if m and f.is_file():
                g = groups.setdefault(m.group(1), {"mt": 0.0, "n": 0})
                g["mt"] = max(g["mt"], f.stat().st_mtime)
                g["n"] += _clips_len(f) or 0
    if groups:
        best = max(groups.items(), key=lambda kv: kv[1]["mt"])
        out["voice"] = best[1]["n"]
        if not out["session"]:
            out["session"] = best[0]
    return out


# ---------------------------------------------------------------- 小组件

def _hex(rgb):
    return "#%02x%02x%02x" % tuple(rgb)


def _rgb(color):
    return tuple(int(color[i:i + 2], 16) for i in (1, 3, 5))


class RoundButton(tk.Canvas):
    """珊瑚粉渐变圆角主按钮（Canvas 绘制，替代 ttk 按钮）。"""

    def __init__(self, master, text="▶  开始跑链", cmd=None,
                 colors=("#F08A85", "#E8663C"), height=42, **kw):
        # width=10：Canvas 默认请求宽高达 378px，会把同行后续按钮挤成
        # 0 宽不显示；给小请求宽，真实尺寸由 pack(fill=x, expand) 分配
        super().__init__(master, width=10, height=height,
                         highlightthickness=0, **kw)
        self.cmd, self.text, self.colors = cmd, text, colors
        self._hover = False
        self.bind("<Configure>", lambda e: self._paint())
        self.bind("<ButtonRelease-1>", lambda e: cmd and cmd())
        self.bind("<Enter>", lambda e: (setattr(self, "_hover", True),
                                        self._paint()))
        self.bind("<Leave>", lambda e: (setattr(self, "_hover", False),
                                        self._paint()))

    def set_action(self, text, colors):
        self.text, self.colors = text, colors
        self._paint()

    def _paint(self):
        w, h = self.winfo_width(), self.winfo_height()
        if w < 20:
            return
        self.delete("all")
        r, n = 13, 36
        top = [min(255, round(v * 1.06)) for v in _rgb(self.colors[0])] \
            if self._hover else list(_rgb(self.colors[0]))
        bot = list(_rgb(self.colors[1]))

        def col_at(t):
            return _hex([round(a + (b - a) * t) for a, b in zip(top, bot)])

        for i in range(n):                        # 垂直渐变条带 + 圆角内缩
            t0, t1 = i / n, (i + 1) / n
            y0, y1 = round(t0 * h), round(t1 * h)
            mid = r <= (y0 + y1) / 2 <= h - r
            x0 = 0 if mid else r
            self.create_rectangle(x0, y0, w - x0, y1,
                                  fill=col_at((t0 + t1) / 2), width=0)
        for cx, cy, t in ((r, r, r / h), (w - r, r, r / h),
                          (r, h - r, 1 - r / h), (w - r, h - r, 1 - r / h)):
            self.create_oval(cx - r, cy - r, cx + r, cy + r,
                             fill=col_at(t), width=0)
        self.create_text(w // 2, h // 2, text=self.text, fill="#FFFFFF",
                         font=(FONT, 12, "bold"))


# ---------------------------------------------------------------- 宠物窗口

class PetWindow(tk.Tk):
    def __init__(self, address=""):
        super().__init__()
        self.title("陪玩大师姐")
        self.overrideredirect(True)
        self.attributes("-topmost", True)
        try:                                       # 魔法色透明：外边距+圆角
            self.attributes("-transparentcolor", C["magic"])
            self._alpha = True
        except tk.TclError:
            self._alpha = False
        self.configure(bg=C["magic"])

        self.proc = None
        self.q = queue.Queue()
        self.parser = ProgressParser()
        self.state = "idle"          # idle/run/done/err/stop
        self.collapsed = False
        self.recorder = None        # 桌面录制器（screenrec.ScreenRecorder）
        self.rec_q = queue.Queue()  # 录制事件（工作线程 → UI 线程）
        self._rec_t0 = 0.0
        self._imgs = {}              # PhotoImage 引用防 GC
        self._logs = []              # 尾部日志（悬停阶段卡显示）
        self._status_text = ""
        self._status_sticky = False
        self._retry_skip = None       # 出错后续跑要跳过的已完成阶段
        self._last_addr = ""
        self._tip_win = None
        self._snap_queue = []
        self.tenant = load_tenant()
        hist = self._load_hist()
        self.run_eval_var = tk.BooleanVar(value=hist["run_eval"])

        self._build()
        self._place_bottom_right()
        if address:
            self.addr_var.set(address)
        elif hist["addresses"]:
            self.addr_var.set(hist["addresses"][0])

        self.after(120, self._poll)
        self.after(2500, self._dev_snapshot)

    # ---------- 布局

    def _build(self):
        margin = 10 if self._alpha else 0           # 透明外边距=悬浮感
        self.outer = tk.Frame(self, bg=C["magic"])
        self.outer.pack(fill="both", expand=True, padx=margin, pady=margin)
        self.card = tk.Frame(self.outer, bg=C["bg"])
        self.card.pack(fill="both", expand=True)
        self._corners()

        # ---- 标题栏（拖动区；— 收起 / ✕ 退出）----
        self.header = tk.Frame(self.card, bg=C["hdr"], height=38)
        self.header.pack(fill="x")
        self.header.pack_propagate(False)
        htxt = tk.Frame(self.header, bg=C["hdr"])
        htxt.pack(side="left", fill="y", pady=4, padx=(12, 0))
        tk.Label(htxt, text="陪玩大师姐 · 跑链小卡", bg=C["hdr"], fg=C["fg"],
                 font=(FONT, 10, "bold"), anchor="w").pack(fill="x")
        self.tenant_lab = tk.Label(htxt, text=f"● 租户 {self.tenant}",
                                   bg=C["hdr"], fg=C["accent"],
                                   font=(FONT, 8), anchor="w")
        self.tenant_lab.pack(fill="x")
        for txt, cmd in (("—", self._toggle_collapse), ("✕", self._on_close)):
            b = tk.Label(self.header, text=txt, bg=C["hdr"], fg=C["dim"],
                         font=(FONT, 10, "bold"), width=3, cursor="hand2")
            b.pack(side="right", padx=1)
            b.bind("<ButtonRelease-1>", lambda e, c=cmd: c())
            b.bind("<Enter>", lambda e, w=b: w.config(fg=C["bad"]))
            b.bind("<Leave>", lambda e, w=b: w.config(fg=C["dim"]))
        # 录制中徽章（● REC 分:秒，随录制显隐）
        self.rec_badge = tk.Label(self.header, text="● 00:00", bg=C["hdr"],
                                  fg=C["bad"], font=(FONT, 9, "bold"))
        for w in (self.header, *self.header.winfo_children()):
            w.bind("<Button-1>", self._drag_start)
            w.bind("<B1-Motion>", self._drag_move)

        # ---- 状态行（默认隐藏；提示闪现/出错常驻/收起态迷你进度）----
        self.status_lab = tk.Label(self.card, text="", bg=C["cream"],
                                   fg=C["lilac"], font=(FONT, 9), anchor="w",
                                   wraplength=W - 40, justify="left")
        self._status_sticky = False

        # ---- 主体 ----
        self.body = tk.Frame(self.card, bg=C["bg"])
        self.body.pack(fill="both", expand=True)

        # 战绩条
        loot = tk.Frame(self.body, bg="#FBF0EA")
        loot.pack(fill="x", padx=12, pady=(8, 6))
        tk.Label(loot, text="最近场次", bg="#FBF0EA", fg=C["lilac"],
                 font=(FONT, 8, "bold")).pack(side="left", padx=(10, 6))
        self.loot_lab = tk.Label(loot, text="击倒 - · 拾取 - · 语音 -",
                                 bg="#FBF0EA", fg=C["fg"], font=(FONT, 9))
        self.loot_lab.pack(side="left", padx=2)

        # 阶段时间线（悬停显示日志尾行）
        prog = tk.Frame(self.body, bg=C["milk"], highlightthickness=1,
                        highlightbackground=C["line"])
        prog.pack(fill="x", padx=12)
        tk.Label(prog, text="识 别 链 进 展", bg=C["milk"], fg=C["lilac"],
                 font=(FONT, 8, "bold")).grid(row=0, column=0, columnspan=2,
                                              sticky="w", padx=10, pady=(6, 2))
        self.stage_labels = {}
        for i, (name, disp) in enumerate(STAGES):
            lab = tk.Label(prog, text=f"○  {disp}", bg=C["milk"], fg=C["dim"],
                           font=(FONT, 9), anchor="w")
            lab.grid(row=i + 1, column=0, sticky="w", padx=(10, 2), pady=1)
            sec = tk.Label(prog, text="", bg=C["milk"], fg=C["good"],
                           font=(FONT, 8))
            sec.grid(row=i + 1, column=1, sticky="e", padx=(2, 10), pady=1)
            self.stage_labels[name] = (lab, sec)
        prog.grid_columnconfigure(0, weight=1)
        for w in (prog, *prog.winfo_children()):
            w.bind("<Enter>", lambda e: self._log_tooltip(True))
            w.bind("<Leave>", lambda e: self._log_tooltip(False))

        # 配置卡：录像地址 + 租户 ID + 推评估
        cfg = tk.Frame(self.body, bg=C["bg"], highlightthickness=1,
                       highlightbackground=C["line"])
        cfg.pack(fill="x", padx=12, pady=(10, 0))
        tk.Label(cfg, text="录像地址（BV号 / URL / 场次目录）", bg=C["bg"],
                 fg=C["lilac"], font=(FONT, 8, "bold")).pack(anchor="w",
                                                            padx=10, pady=(7, 2))
        row = tk.Frame(cfg, bg=C["bg"])
        row.pack(fill="x", padx=10)
        self.addr_var = tk.StringVar()
        self.addr_cb = ttk.Combobox(row, textvariable=self.addr_var,
                                    values=self._load_hist()["addresses"],
                                    font=(FONT, 9))
        self.addr_cb.pack(side="left", fill="x", expand=True)
        browse = tk.Label(row, text="浏览", bg=C["milk"], fg=C["fg"],
                          font=(FONT, 8), padx=7, pady=3, cursor="hand2",
                          highlightthickness=1, highlightbackground=C["line"])
        browse.pack(side="left", padx=(6, 0))
        browse.bind("<ButtonRelease-1>", lambda e: self._browse())

        tk.Label(cfg, text="租户 ID（推评估包用，保存写入 config.yaml）",
                 bg=C["bg"], fg=C["lilac"], font=(FONT, 8, "bold")
                 ).pack(anchor="w", padx=10, pady=(12, 2))
        row2 = tk.Frame(cfg, bg=C["bg"])
        row2.pack(fill="x", padx=10)
        self.tenant_var = tk.StringVar(value=self.tenant)
        ent = tk.Entry(row2, textvariable=self.tenant_var, font=(FONT, 9),
                       bg=C["milk"], relief="flat", highlightthickness=1,
                       highlightbackground=C["line"])
        ent.pack(side="left", fill="x", expand=True, ipady=3)
        save = tk.Label(row2, text="保存", bg=C["milk"], fg=C["accent"],
                        font=(FONT, 8, "bold"), padx=8, pady=3, cursor="hand2",
                        highlightthickness=1, highlightbackground=C["line"])
        save.pack(side="left", padx=(6, 0))
        save.bind("<ButtonRelease-1>", lambda e: self._save_tenant())

        opt = tk.Frame(cfg, bg=C["bg"])
        opt.pack(fill="x", padx=10, pady=(8, 8))
        tk.Checkbutton(opt, text="推评估并打标（--run-eval）",
                       variable=self.run_eval_var, bg=C["bg"], fg=C["fg"],
                       activebackground=C["bg"], activeforeground=C["fg"],
                       selectcolor=C["milk"], font=(FONT, 8)).pack(side="left")

        # 录制结束后的快捷入口：对刚录场次一键启动识别链（自动剪辑）
        self.auto_btn = RoundButton(self.body, text="✂  启动自动剪辑",
                                    cmd=self._auto_edit, height=42,
                                    colors=("#9B6B8F", "#7A5280"))
        # 主按钮行：录桌面（录制中变为 暂停+结束）/ 开始跑链
        btnrow = tk.Frame(self.body, bg=C["bg"])
        btnrow.pack(fill="x", padx=12, pady=(10, 8))
        self._btnrow = btnrow
        self.rec_area = tk.Frame(btnrow, bg=C["bg"])
        self.rec_area.pack(side="left", fill="x", expand=True)
        self.rec_btn = RoundButton(self.rec_area, text="●  录桌面",
                                   cmd=self._toggle_record, height=42,
                                   colors=("#F3B7AD", "#E8756B"))
        self.rec_btn.pack(fill="x", expand=True)
        self.pause_btn = RoundButton(self.rec_area, text="⏸  暂停",
                                     cmd=self._toggle_pause, height=42,
                                     colors=("#F2C14E", "#D99A3C"))
        self.end_btn = RoundButton(self.rec_area, text="■  结束",
                                   cmd=self._end_record, height=42,
                                   colors=("#D9534F", "#B93E3A"))
        self.btn = RoundButton(btnrow, cmd=self._on_main_button, height=42)
        self.btn.pack(side="left", fill="x", expand=True, padx=(8, 0))
        tk.Label(self.body, text="", bg=C["bg"]).pack()   # 底部呼吸空间
        self._fit_height()

    def _show_auto(self):
        """显示「启动自动剪辑」（录完场次后），置于录桌面按钮上方。"""
        if not self.auto_btn.winfo_ismapped():
            self.auto_btn.pack(fill="x", padx=12, pady=(10, 0),
                               before=self._btnrow)
            self._fit_height()

    def _hide_auto(self):
        if self.auto_btn.winfo_ismapped():
            self.auto_btn.pack_forget()
            self._fit_height()

    def _auto_edit(self):
        """对地址栏里的刚录场次启动全链（下载→…→推评估包）。"""
        self._hide_auto()
        self._toggle_run()

        self._refresh_idle_stats()

    def _corners(self):
        """四角魔法色贴片：透明生效时窗口呈圆角悬浮卡。"""
        if not self._alpha:
            return
        try:
            for n, kw in (("corner_tl", dict(x=0, y=0)),
                          ("corner_tr", dict(relx=1, y=0, anchor="ne")),
                          ("corner_bl", dict(x=0, rely=1, anchor="sw")),
                          ("corner_br", dict(relx=1, rely=1, anchor="se"))):
                img = tk.PhotoImage(file=str(ASSETS / f"{n}.png"))
                self._imgs[n] = img
                tk.Label(self.card, image=img, bg=C["magic"]).place(**kw)
        except Exception:
            pass

    # ---------- 拖动 / 收起

    def _drag_start(self, e):
        self._dx = e.x_root - self.winfo_x()
        self._dy = e.y_root - self.winfo_y()

    def _drag_move(self, e):
        if hasattr(self, "_dx"):
            self.geometry(f"+{e.x_root - self._dx}+{e.y_root - self._dy}")

    def _fit_height(self):
        """按当前内容自适应高度（收起/展开切换用）。"""
        self.update_idletasks()
        h = self.card.winfo_reqheight() + (20 if self._alpha else 0)
        self.geometry(f"{W}x{h}")
        return h

    def _place_bottom_right(self):
        h = self._fit_height()
        sw, sh = self.winfo_screenwidth(), self.winfo_screenheight()
        self.geometry(f"+{sw - W - 28}+{sh - h - 88}")

    def _toggle_collapse(self):
        self.collapsed = not self.collapsed
        if self.collapsed:
            self.body.pack_forget()
            if self.state == "run":                # 收起态保留迷你进度行
                self._show_status(self._status_text or "跑链 0/8",
                                  C["accent"], sticky=True)
        else:
            self.body.pack(fill="both", expand=True)
            if self.state == "run":                # 展开后时间线接管，收起行
                self._hide_status()
            elif not self._status_sticky:
                self._hide_status()
        self._fit_height()

    # ---------- 历史

    def _load_hist(self):
        try:
            d = json.loads(HIST.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            d = {}
        return {"addresses": d.get("addresses", []),
                "run_eval": bool(d.get("run_eval", False))}

    def _save_hist(self):
        HIST.parent.mkdir(parents=True, exist_ok=True)
        addr = self.addr_var.get().strip()
        addrs = [a for a in self._load_hist()["addresses"] if a != addr]
        if addr:
            addrs.insert(0, addr)
        HIST.write_text(json.dumps(
            {"addresses": addrs[:8], "run_eval": bool(self.run_eval_var.get()),
             "tenant": self.tenant},
            ensure_ascii=False, indent=1), encoding="utf-8")

    # ---------- 租户

    def _save_tenant(self):
        v = self.tenant_var.get().strip()
        if not v:
            self._flash("租户 ID 不能为空", C["bad"])
            return
        if self.proc and self.proc.poll() is None:
            self._flash("跑链中不能改租户哦", C["warn"])
            return
        try:
            save_tenant(v)
        except OSError as e:
            self._flash(f"保存失败：{e}", C["bad"])
            return
        self.tenant = v
        self.tenant_lab.config(text=f"● 租户 {v}")
        LOG.info("租户已保存：%s", v)
        self._flash(f"租户已保存：{v} ✓", C["good"])

    # ---------- 桌面录制

    def _set_rec_ui(self, mode):
        """按钮区三态：idle（录桌面+开始跑链）/ rec（暂停+结束）/ paused（继续+结束）。"""
        for b in (self.rec_btn, self.pause_btn, self.end_btn):
            b.pack_forget()
        if mode == "idle":
            self.rec_btn.pack(fill="x", expand=True)
            self.btn.pack(side="left", fill="x", expand=True, padx=(8, 0))
            self.rec_badge.pack_forget()
            self.rec_btn.set_action("●  录桌面", ("#F3B7AD", "#E8756B"))
        else:
            self.btn.pack_forget()               # 录制期间让整行给录控
            self.rec_badge.pack(side="right", padx=(2, 4))
            self.pause_btn.pack(side="left", fill="x", expand=True)
            self.end_btn.pack(side="left", fill="x", expand=True, padx=(8, 0))
            if mode == "paused":
                self.pause_btn.set_action("▶  继续", ("#4CA37E", "#3B8A66"))
            else:
                self.pause_btn.set_action("⏸  暂停", ("#F2C14E", "#D99A3C"))

    def _toggle_record(self):
        if self.recorder and self.recorder.recording:
            return                                # 录制中由暂停/结束接管
        self._hide_auto()
        from toolbox.screenrec import ScreenRecorder
        self.recorder = ScreenRecorder(self._rec_root(),
                                       load_config().get("screenrec") or {},
                                       on_event=self._on_rec_event)
        self._rec_t0 = time.time()
        self.recorder.start()
        self._set_rec_ui("rec")
        self._tick_rec()

    def _on_rec_event(self, kind, payload):
        """录制事件入队（put(item, block) 签名陷阱：必须包成单元素元组）。"""
        self.rec_q.put((kind, payload))

    def _toggle_pause(self):
        if not (self.recorder and self.recorder.recording):
            return
        if self.recorder.paused:
            self.recorder.resume()
        else:
            self.recorder.pause()

    def _end_record(self):
        if self.recorder and self.recorder.recording:
            self.recorder.stop()

    def _rec_root(self):
        """录像地址字段是目录就录到那里，否则默认 video/。

        场次目录（里面是分P mp4 的叶子目录）不当输出根——否则日期
        场次目录会嵌进直播场次里，破坏「根/日期/分P」两级结构。
        """
        addr = self.addr_var.get().strip()
        if addr and Path(addr).is_dir():
            p = Path(addr)
            if not any(p.glob("* P[0-9]* *.mp4")):
                return p
        return ROOT / "video"

    def _tick_rec(self):
        """录制中每秒刷新徽章/按钮上的有效时长。"""
        if not (self.recorder and self.recorder.recording):
            return
        el = int(self.recorder.elapsed())
        txt = f"{el // 60:02d}:{el % 60:02d}"
        if self.recorder.paused:
            self.rec_badge.config(text=f"⏸ {txt}", fg=C["warn"])
            self.pause_btn.set_action("▶  继续", ("#4CA37E", "#3B8A66"))
        else:
            self.rec_badge.config(text=f"● {txt}", fg=C["bad"])
            self.pause_btn.set_action("⏸  暂停", ("#F2C14E", "#D99A3C"))
        self.after(1000, self._tick_rec)

    def _handle_rec_event(self, kind, payload):
        LOG.info("rec事件 %s %s", kind,
                 {k: v for k, v in payload.items() if k != "files"})
        if kind == "start":
            name = Path(payload["session"]).name
            if self.state == "idle":              # 跑链状态优先占状态行
                self._flash(
                    f"录制中 P{payload['part']} → {name}"
                    f"（{'系统声音' if payload['audio'] else '纯视频'}）",
                    C["accent"])
        elif kind == "pause":
            self._set_rec_ui("paused")
            if self.state == "idle":
                self._flash(f"已暂停（有效 {payload.get('elapsed', 0):.0f}s），"
                            "点「继续」接着录", C["warn"], sticky=True)
        elif kind == "resume":
            self._set_rec_ui("rec")
            if self.state == "idle":
                self._flash(f"继续录制 → P{payload.get('part', '?')}",
                            C["accent"])
        elif kind == "stop":
            self._set_rec_ui("idle")
            session = payload.get("session", "")
            if session and Path(session).is_dir():
                self.addr_var.set(session)        # 录完直接可跑链
                self._save_hist()
            if payload.get("files"):
                self._show_auto()                 # 「启动自动剪辑」快捷入口
            if self.state == "idle":
                self._flash(f"已录制 {payload.get('secs', 0)}s，"
                            f"{len(payload.get('files', []))} 个分P → "
                            f"{Path(session).name if session else ''}",
                            C["good"])
        elif kind == "error":
            self._flash(payload.get("msg", "录制异常"), C["warn"])

    # ---------- 开跑 / 停止

    def _browse(self):
        d = filedialog.askdirectory(initialdir=str(ROOT / "video"),
                                    title="选择场次目录（含 P1..PN 分片）")
        if d:
            self.addr_var.set(str(Path(d)))

    def _toggle_run(self):
        if self.proc and self.proc.poll() is None:
            self._stop()
            return
        addr = self.addr_var.get().strip()
        if not addr:
            self._flash("先把录像地址填上呀", C["warn"])
            return
        self._start(addr)

    def _on_main_button(self):
        """主按钮分发：出错态=续跑（--skip 已完成阶段），其余=开始/停止。"""
        if self.state == "err" and self._retry_skip:
            addr = self._last_addr or self.addr_var.get().strip()
            if addr:
                self._start(addr, skip=self._retry_skip)
                return
        self._toggle_run()

    def _start(self, addr, skip=()):
        self._save_hist()
        self.parser = ProgressParser()
        self._logs = []
        self._retry_skip = None
        self._last_addr = addr
        for name, disp in STAGES:
            self.stage_labels[name][0].config(text=f"○  {disp}", fg=C["dim"])
            self.stage_labels[name][1].config(text="")
        self._hide_auto()                         # 跑链已启动，快捷入口让位
        self._hide_status()
        self._set_state("run", f"开跑：{Path(addr).name[:26]}")
        args = [sys.executable, "-X", "utf8", "-m", "toolbox", "pipeline", addr]
        if skip:
            args += ["--skip", *skip]
        if self.run_eval_var.get():
            args.append("--run-eval")
        LOG.info("跑链启动：%s（skip=%s run_eval=%s）",
                 addr, list(skip) or "无", self.run_eval_var.get())
        env = dict(os.environ, PYTHONUTF8="1", PYTHONIOENCODING="utf-8")
        self.proc = subprocess.Popen(args, cwd=str(ROOT), env=env,
                                     stdout=subprocess.PIPE,
                                     stderr=subprocess.STDOUT,
                                     text=True, encoding="utf-8",
                                     errors="replace")
        threading.Thread(target=self._reader, daemon=True).start()
        self.btn.set_action("■  停止", ("#D9534F", "#B93E3A"))

    def _reader(self):
        for line in self.proc.stdout:
            self.q.put(line.rstrip("\n"))
        self.q.put(None)            # EOF 哨兵

    def _kill_tree(self):
        if self.proc and self.proc.poll() is None:
            if os.name == "nt":     # pipeline 会再派生 ffmpeg/扫描子进程
                subprocess.run(["taskkill", "/PID", str(self.proc.pid),
                                "/T", "/F"], capture_output=True)
            else:
                self.proc.terminate()

    def _stop(self):
        self.state = "stop"         # 抢在 _finish 前定性，EOF 到达后不再覆盖
        self._kill_tree()
        self._set_state("stop", "已手动停止，素材我先收好…")
        self.btn.set_action("▶  开始跑链", ("#F08A85", "#E8663C"))

    # ---------- 轮询与渲染

    def _poll(self):
        drained = False
        try:
            while True:
                line = self.q.get_nowait()
                if line is None:    # 进程收尾
                    self._finish()
                    break
                self.parser.update(line)
                self._logs = (self._logs + [line])[-15:]
                LOG_PIPE.info("%s", line)         # 全量落盘，排障可查
                drained = True
        except queue.Empty:
            pass
        while True:                 # 桌面录制事件（工作线程投递）
            try:
                kind, payload = self.rec_q.get_nowait()
            except queue.Empty:
                break
            try:
                self._handle_rec_event(kind, payload)
            except Exception as e:  # 单个坏事件不杀轮询链
                LOG.error("rec事件处理异常 %s：%s", kind, e)
        if drained:
            self._render()
        self.after(120, self._poll)

    def _finish(self):
        if self.state == "stop":
            return
        code = self.proc.poll() if self.proc else 0
        if code == 0 and self.parser.finished:
            st = latest_stats()
            name = (st["session"] or self.parser.session or "")[:18]
            self._set_state("done", f"完成！{name} 击倒{_or_dash(st['knockdowns'])} "
                                    f"拾取{_or_dash(st['pickups'])} "
                                    f"语音{_or_dash(st['voice'])}")
            LOG.info("跑链完成：%s", self._status_text)
        else:
            why = self.parser.error_line or f"退出码 {code}，看日志排查"
            self._set_state("err", f"出错：{why}")
            self._retry_skip = [name for name, _ in STAGES
                                if self.parser.stages[name]["status"]
                                in ("done", "skip")]
            LOG.warning("跑链失败：rc=%s err=%s 可续跑（跳过 %d 个已完成阶段）",
                        code, why, len(self._retry_skip))
        self.btn.set_action("↻  续跑" if self.state == "err"
                            else "▶  开始跑链", ("#F08A85", "#E8663C"))
        self._refresh_idle_stats()

    def _render(self):
        p = self.parser
        if self.state == "run":                    # 收起态迷你进度文字
            bits = [f"跑链 {p.done_count()}/{len(STAGES)}"]
            cur = p.running_stage()
            if cur:
                bits.append(f"{cur}中")
            if p.session:
                bits.append(p.session[:12])
            self._set_status(" · ".join(bits), C["accent"])
        for name, disp in STAGES:
            st = p.stages[name]
            mark, key = GLYPH[st["status"]]
            lab, sec = self.stage_labels[name]
            lab.config(text=f"{mark}  {disp}", fg=C[key])
            sec.config(text=f"{st['secs']}s" if st["secs"] is not None else "",
                       fg=C["good"] if st["status"] == "done" else C["dim"])
        if p.counts["knockdowns"] is not None or p.counts["pickups"] is not None:
            self.loot_lab.config(
                text=f"击倒 {_or_dash(p.counts['knockdowns'])} · "
                     f"拾取 {_or_dash(p.counts['pickups'])}")

    # ---------- 状态行（闪现式提示；出错常驻；收起态作迷你进度条）----------

    def _set_state(self, state, status):
        self.state = state
        if state == "err":
            self._show_status(status, C["bad"], sticky=True)
        elif state == "run":
            if not self.collapsed:
                self._hide_status()               # 展开态进展由时间线呈现
        else:                                      # idle/done/stop 短闪提示
            if status:
                self._flash(status, C[STATE_COLOR.get(state, "dim")])
            else:
                self._hide_status()

    def _show_status(self, text, color=None, sticky=False):
        self._status_text = text
        self._status_sticky = sticky
        self.status_lab.config(text=text, fg=color or C["lilac"])
        if not self.status_lab.winfo_ismapped():
            kw = dict(fill="x", padx=12, pady=(8, 0), ipady=4)
            try:                                   # 展开态：插在主体上方
                self.status_lab.pack(before=self.body, **kw)
            except tk.TclError:                    # 收起态 body 未铺，排标题栏下
                self.status_lab.pack(**kw)
            self._fit_height()

    def _hide_status(self):
        self._status_sticky = False
        if self.status_lab.winfo_ismapped():
            self.status_lab.pack_forget()
            self._fit_height()

    def _set_status(self, text, color=None):
        """只改文字不改可见性（收起态迷你进度刷新用）。"""
        self._status_text = text
        self.status_lab.config(text=text, fg=color or C["lilac"])

    def _flash(self, text, color=None, sticky=False):
        """状态行短闪（toast），2.5s 后自动收起；sticky=True 常驻到下次动作。"""
        self._show_status(text, color or C["accent"], sticky=sticky)
        if not sticky:
            self.after(2500, lambda: self._hide_status()
                       if self._status_text == text else None)

    def _refresh_idle_stats(self):
        st = latest_stats()
        if st["session"]:
            self.loot_lab.config(
                text=f"{st['session'][:16]}｜击倒 {_or_dash(st['knockdowns'])} · "
                     f"拾取 {_or_dash(st['pickups'])} · "
                     f"语音 {_or_dash(st['voice'])}", fg=C["fg"])
        else:
            self.loot_lab.config(text="还没有场次产物，跑一场就有了",
                                 fg=C["dim"])

    def _log_tooltip(self, show):
        if show and self._logs:
            if self._tip_win is None or not self._tip_win.winfo_exists():
                tw = tk.Toplevel(self)
                tw.wm_overrideredirect(True)
                tw.attributes("-topmost", True)
                f = tk.Frame(tw, bg="#FFFFFF", highlightthickness=1,
                             highlightbackground=C["line"])
                f.pack()
                tk.Label(f, text="\n".join(self._logs[-10:]), bg="#FFFFFF",
                         fg=C["dim"], font=("Consolas", 8), justify="left",
                         anchor="w", wraplength=300).pack(padx=8, pady=6)
                self._tip_win = tw
            self._tip_win.geometry(
                f"+{self.winfo_rootx() + 40}+{self.winfo_rooty() + 200}")
        elif self._tip_win is not None and self._tip_win.winfo_exists():
            self._tip_win.destroy()
            self._tip_win = None

    # ---------- 调试快照（PET_SNAPSHOT=<目录> 时各状态截屏）

    def _dev_snapshot(self):
        snapdir = os.environ.get("PET_SNAPSHOT")
        if not snapdir:
            return
        try:
            from PIL import ImageGrab
        except ImportError:
            return
        Path(snapdir).mkdir(parents=True, exist_ok=True)
        # tkinter 逻辑坐标 vs ImageGrab 物理像素：按屏幕宽比值换算（DPI≠100%）
        full = ImageGrab.grab()
        k = full.size[0] / max(1, self.winfo_screenwidth())

        def grab(name):
            self.update_idletasks()
            self.update()
            x, y = round(self.winfo_rootx() * k), round(self.winfo_rooty() * k)
            w = round(self.winfo_width() * k)
            h = round(self.winfo_height() * k)
            ImageGrab.grab(bbox=(x, y, x + w, y + h)).save(
                Path(snapdir) / f"{name}.png")

        def demo_run():
            p = self.parser
            p.session, p.parts = "直接跑刀2小时一千三", 2
            for n, s in (("download", 86), ("asr", 540), ("speaker", 95)):
                p.stages[n] = {"status": "done", "secs": s}
            p.stages["detect"] = {"status": "run", "secs": None}
            p.counts.update(knockdowns=12, pickups=None)
            self._set_state("run", "")
            self._render()

        def demo_done():
            self._set_rec_ui("idle")                # 复位录制视觉
            self._hide_auto()
            for i, (n, _) in enumerate(STAGES):
                self.parser.stages[n] = {"status": "done", "secs": 60 + i * 37}
            self._set_state("done", "完成！击倒14 拾取9 语音6")

        def demo_rec():                             # 录制中（仅视觉，不真录）
            self._set_rec_ui("rec")
            self.rec_badge.config(text="● 02:14", fg=C["bad"])
            self._set_state("idle", "录制中 P1 → 2026-09-22 桌面录制（系统声音）")

        def demo_recpause():                        # 已暂停（仅视觉）
            self._set_rec_ui("paused")
            self.rec_badge.config(text="⏸ 02:14", fg=C["warn"])
            self._set_state("idle", "已暂停（有效 134s），点「继续」接着录")

        def demo_afterstop():                       # 录制刚结束（仅视觉）
            self._set_rec_ui("idle")
            self.addr_var.set("video/2026-09-22 桌面录制")
            self._show_auto()
            self._set_state("idle", "已录制 134s，1 个分P → 2026-09-22 桌面录制")

        self._snap_queue = [
            ("idle", lambda: self._set_state("idle", "")),
            ("run", demo_run),
            ("rec", demo_rec),
            ("recpause", demo_recpause),
            ("afterstop", demo_afterstop),
            ("done", demo_done),
            ("err", lambda: self._set_state("err", "出错：第4步 OCR，看日志排查")),
        ]

        def step():
            if not self._snap_queue:
                return
            name, fn = self._snap_queue.pop(0)
            fn()
            self.after(300, lambda: _shot(name))

        def _shot(name):
            grab(name)
            if name == "run":                      # 顺带截收起态
                self._toggle_collapse()
                grab("collapsed")
                self._toggle_collapse()
            self.after(300, step)

        step()

    def _on_close(self):
        if self.recorder and self.recorder.recording:
            if not messagebox.askokcancel(
                    "还在录", "桌面录制进行中，退出会结束录制，确定？"):
                return
            self.recorder.stop(timeout=8)
        if self.proc and self.proc.poll() is None:
            if not messagebox.askokcancel(
                    "还在跑", "识别链仍在运行，退出会终止整条链，确定？"):
                return
            self.state = "stop"
            self._kill_tree()
        self._save_hist()
        self.destroy()


def run(address=""):
    """GUI 入口（cli: python -m toolbox pet [--address ...]）。"""
    PetWindow(address=address).mainloop()
