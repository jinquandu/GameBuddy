"""桌面宠物客户端：置顶小窗呈现识别链进展，配置视频地址一键开跑。

python -m toolbox pet [--address <BV号/URL/场次目录>]

设计（2026-09-20）：
- 无边框置顶小窗（overrideredirect + topmost），按住标题栏拖动，
  双击标题栏/点「—」收起成宠物条，✕ 退出（运行中先确认并杀整棵子进程）。
- 进展呈现三层：8 阶段勾选表（下载→…→推评估包）+ 当前阶段细目行 +
  滚动日志尾行；全部由解析 [pipeline] 阶段标记与各链路 print 得来，
  详见 ProgressParser。完成后读 data/ 三线 JSON 汇总 击倒/拾取/语音。
- 视频地址：输入框 + 历史下拉（data/pet/history.json 持久化）+ 浏览目录
  + 「推评估并打标」勾选（对应 pipeline --run-eval），开始/停止按钮。
- 宠物状态：😴 待命中 / 🏃 跑链中（跳动）/ 🎉 完成 / 😵 出错 / 🛑 已停止。
  空闲时显示最近一场的三线计数（mtime 最新即刚跑完的那场）。
"""
import json
import math
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

from toolbox.config import DATA, ROOT

# ---------------------------------------------------------------- 常量

STAGES = [  # (阶段名, 显示名) —— 与 pipeline.run_pipeline 的 stage 顺序一致
    ("download", "下载"), ("asr", "转写"), ("speaker", "说话人"),
    ("detect", "OCR检测"), ("knockdowns", "击倒切片"), ("pickups", "拾取切片"),
    ("voice", "语音评估"), ("push", "推评估包"),
]
GLYPH = {"pending": ("○", "dim"), "run": ("▶", "accent"),
         "done": ("✓", "good"), "skip": ("—", "dim")}
STATE_COLOR = {"idle": "dim", "run": "accent", "done": "good",
               "err": "bad", "stop": "warn"}
MASCOT = {"idle": "🐹", "run": "🏃", "done": "🎉", "err": "😵", "stop": "🛑"}

C = {"bg": "#101418", "card": "#1a2027", "line": "#2a323c",   # 打标站同款暗色
     "fg": "#dce3ea", "dim": "#8b98a5", "good": "#4cc38a",
     "warn": "#e5c454", "bad": "#e5534b", "accent": "#ff7b54"}
FONT = "Microsoft YaHei UI"
W, H_FULL, H_MIN = 312, 502, 116   # 全尺寸 / 收起后高度

HIST = DATA / "pet" / "history.json"


def _or_dash(v):
    return "-" if v is None else v


# ---------------------------------------------------------------- 日志解析

class ProgressParser:
    """把 pipeline 子进程 stdout 行解析成 UI 状态；喂全量行即可。

    解析目标（与 pipeline.py / 各链路 print 格式一一对应）：
      [pipeline] {name} 开始 ... / {name} 完成 ({n}s) / -- 跳过 {name}
      [pipeline] 场次：{session}，{n} 个分片 / 全链完成。
      共 {n} 个击倒片段 / 共 {n} 个拾取会话片段
    其余信息行择要进「细目行」（完成/复用/粗簇/触发服务端等）。
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

    vroot = DATA / "voice"                       # 语音目录按 <场次>_P<k> 前缀分组
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


# ---------------------------------------------------------------- 宠物窗口

class PetWindow(tk.Tk):
    def __init__(self, address=""):
        super().__init__()
        self.title("大世界·识别链宠物")
        self.configure(bg=C["bg"])
        self.overrideredirect(True)
        self.attributes("-topmost", True)

        self.proc = None
        self.q = queue.Queue()
        self.parser = ProgressParser()
        self.state = "idle"          # idle/run/done/err/stop
        self.collapsed = False
        self._t0 = time.time()
        hist = self._load_hist()
        self.run_eval_var = tk.BooleanVar(value=hist["run_eval"])

        self._place_bottom_right()
        self._build()
        if address:
            self.addr_var.set(address)
        elif hist["addresses"]:
            self.addr_var.set(hist["addresses"][0])

        self.after(120, self._poll)
        self.after(16, self._animate)

    # ---------- 布局

    def _place_bottom_right(self):
        self.update_idletasks()
        sw, sh = self.winfo_screenwidth(), self.winfo_screenheight()
        self.geometry(f"{W}x{H_FULL}+{sw - W - 28}+{sh - H_FULL - 88}")

    def _build(self):
        # 标题栏（拖动区；— 收起 / ✕ 退出）
        self.header = tk.Frame(self, bg=C["card"], height=34)
        self.header.pack(fill="x")
        self.header.pack_propagate(False)
        tk.Label(self.header, text="🐹 大世界识别链", bg=C["card"], fg=C["fg"],
                 font=(FONT, 10, "bold")).pack(side="left", padx=10)
        for txt, cmd in (("—", self._toggle_collapse), ("✕", self._on_close)):
            b = tk.Label(self.header, text=txt, bg=C["card"], fg=C["fg"],
                         font=(FONT, 10, "bold"), width=3, cursor="hand2")
            b.pack(side="right", padx=1)
            b.bind("<ButtonRelease-1>", lambda e, c=cmd: c())
            b.bind("<Enter>", lambda e, w=b: w.config(fg=C["bad"]))
            b.bind("<Leave>", lambda e, w=b: w.config(fg=C["fg"]))
        for w in (self.header, *self.header.winfo_children()):
            w.bind("<Button-1>", self._drag_start)
            w.bind("<B1-Motion>", self._drag_move)

        self.body = tk.Frame(self, bg=C["bg"])
        self.body.pack(fill="both", expand=True)

        self.pet_cv = tk.Canvas(self.body, bg=C["bg"], highlightthickness=0,
                                height=78)
        self.pet_cv.pack(fill="x", padx=8)
        self.mascot = self.pet_cv.create_text(
            W // 2, 42, text=MASCOT["idle"], font=("Segoe UI Emoji", 30))
        self.bubble = self.pet_cv.create_text(
            W // 2, 71, text="待命中 · 等地址开跑", fill=C["dim"],
            font=(FONT, 9))

        prog = tk.Frame(self.body, bg=C["card"])
        self.stage_labels = {}
        for i, (name, disp) in enumerate(STAGES):
            r, c = divmod(i, 2)
            lab = tk.Label(prog, text=f"○ {disp}", bg=C["card"], fg=C["dim"],
                           font=(FONT, 9), anchor="w")
            lab.grid(row=r, column=c, sticky="w", padx=8, pady=1)
            self.stage_labels[name] = lab
        for c in (0, 1):
            prog.grid_columnconfigure(c, weight=1)

        style = ttk.Style(self)
        style.layout("pet.Horizontal.TProgressbar",
                     style.layout("Horizontal.TProgressbar"))
        style.configure("pet.Horizontal.TProgressbar", troughcolor=C["card"],
                        background=C["accent"], bordercolor=C["line"])
        self.bar = ttk.Progressbar(self.body, maximum=len(STAGES), value=0,
                                   style="pet.Horizontal.TProgressbar")
        self.detail_lab = tk.Label(self.body, text=" ", bg=C["bg"], fg=C["fg"],
                                   font=(FONT, 9), anchor="w", wraplength=286)
        self.log_lab = tk.Label(self.body, text=" ", bg=C["bg"], fg=C["dim"],
                                font=(FONT, 8), anchor="w", wraplength=286)

        # 地址配置区
        cfg = tk.Frame(self.body, bg=C["card"])
        tk.Label(cfg, text="视频地址（BV号 / URL / 场次目录）", bg=C["card"],
                 fg=C["dim"], font=(FONT, 8)).pack(anchor="w", padx=8,
                                                   pady=(4, 0))
        row = tk.Frame(cfg, bg=C["card"])
        row.pack(fill="x", padx=8)
        self.addr_var = tk.StringVar()
        self.addr_cb = ttk.Combobox(row, textvariable=self.addr_var,
                                    values=self._load_hist()["addresses"],
                                    font=(FONT, 9))
        self.addr_cb.pack(side="left", fill="x", expand=True)
        browse = tk.Label(row, text="📁", bg=C["card"], fg=C["fg"],
                          cursor="hand2", font=("Segoe UI Emoji", 11))
        browse.pack(side="left", padx=(4, 0))
        browse.bind("<ButtonRelease-1>", lambda e: self._browse())
        opt = tk.Frame(cfg, bg=C["card"])
        tk.Checkbutton(opt, text="推评估并打标 (--run-eval)",
                       variable=self.run_eval_var, bg=C["card"], fg=C["fg"],
                       activebackground=C["card"], activeforeground=C["fg"],
                       selectcolor=C["bg"], font=(FONT, 8)).pack(side="left")
        opt.pack(fill="x", padx=8, pady=(4, 4))
        self.btn = tk.Label(cfg, text="▶ 开始", bg=C["accent"], fg="#1a1208",
                            font=(FONT, 10, "bold"), width=8, cursor="hand2")
        self.btn.pack(side="right", padx=8, pady=(2, 6))
        self.btn.bind("<ButtonRelease-1>", lambda e: self._toggle_run())

        self.stats_lab = tk.Label(self.body, text=" ", bg=C["bg"], fg=C["dim"],
                                  font=(FONT, 8), anchor="w", justify="left",
                                  wraplength=286)

        # 统一铺排（收起/展开按这份顺序恢复）
        self._body_order = [
            (self.pet_cv, dict(fill="x", padx=8)),
            (prog, dict(fill="x", padx=8, pady=(2, 0))),
            (self.bar, dict(fill="x", padx=10, pady=(6, 2))),
            (self.detail_lab, dict(fill="x", padx=10)),
            (self.log_lab, dict(fill="x", padx=10, pady=(0, 2))),
            (cfg, dict(fill="x", padx=8, pady=(4, 0))),
            (self.stats_lab, dict(fill="x", padx=10, pady=(2, 6))),
        ]
        for w, kw in self._body_order:
            w.pack(**kw)
        self._refresh_idle_stats()

    # ---------- 拖动 / 收起

    def _drag_start(self, e):
        self._dx = e.x_root - self.winfo_x()
        self._dy = e.y_root - self.winfo_y()

    def _drag_move(self, e):
        if hasattr(self, "_dx"):
            self.geometry(f"+{e.x_root - self._dx}+{e.y_root - self._dy}")

    def _toggle_collapse(self):
        self.collapsed = not self.collapsed
        for w in self.body.winfo_children():
            w.pack_forget()
        if self.collapsed:
            self.pet_cv.pack(fill="x", padx=8, pady=(4, 2))
            self.geometry(f"{W}x{H_MIN}")
        else:
            for w, kw in self._body_order:
                w.pack(**kw)
            self.geometry(f"{W}x{H_FULL}")

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
            {"addresses": addrs[:8], "run_eval": bool(self.run_eval_var.get())},
            ensure_ascii=False, indent=1), encoding="utf-8")

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
            self._set_bubble("先把视频地址填上呀 🥺", C["warn"])
            return
        self._start(addr)

    def _start(self, addr):
        self._save_hist()
        self.parser = ProgressParser()
        for name, disp in STAGES:
            self.stage_labels[name].config(text=f"○ {disp}", fg=C["dim"])
        self.bar["value"] = 0
        self.detail_lab.config(text=" ")
        self._set_state("run", f"跑链中：{Path(addr).name[:26]}")
        args = [sys.executable, "-X", "utf8", "-m", "toolbox", "pipeline", addr]
        if self.run_eval_var.get():
            args.append("--run-eval")
        env = dict(os.environ, PYTHONUTF8="1", PYTHONIOENCODING="utf-8")
        self.proc = subprocess.Popen(args, cwd=str(ROOT), env=env,
                                     stdout=subprocess.PIPE,
                                     stderr=subprocess.STDOUT,
                                     text=True, encoding="utf-8",
                                     errors="replace")
        threading.Thread(target=self._reader, daemon=True).start()
        self.btn.config(text="■ 停止", bg=C["bad"], fg="#fff")

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
        self._set_state("stop", "已手动停止")
        self.btn.config(text="▶ 开始", bg=C["accent"], fg="#1a1208")

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
                self.log_lab.config(text=line[-64:])
                drained = True
        except queue.Empty:
            pass
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
        else:
            why = self.parser.error_line or f"退出码 {code}，看日志排查"
            self._set_state("err", f"出错：{why}")
        self.btn.config(text="▶ 开始", bg=C["accent"], fg="#1a1208")
        self._refresh_idle_stats()

    def _render(self):
        p = self.parser
        if p.session and self.state == "run":   # 完成态气泡不被跑链文案覆盖
            self._set_bubble(f"跑链中：{p.session[:20]}（{p.parts} 分片）",
                             C["accent"])
        for name, disp in STAGES:
            st = p.stages[name]
            mark, key = GLYPH[st["status"]]
            extra = f" {st['secs']}s" if st["secs"] is not None else ""
            self.stage_labels[name].config(text=f"{mark} {disp}{extra}",
                                           fg=C[key])
        done = sum(1 for s in p.stages.values()
                   if s["status"] in ("done", "skip"))
        self.bar["value"] = done
        if p.counts["knockdowns"] is not None or p.counts["pickups"] is not None:
            self.detail_lab.config(
                text=f"击倒 {_or_dash(p.counts['knockdowns'])} · "
                     f"拾取 {_or_dash(p.counts['pickups'])}｜{p.detail[:44]}")
        elif p.detail:
            self.detail_lab.config(text=p.detail[:60])

    # ---------- 状态与动画

    def _set_state(self, state, bubble):
        self.state = state
        self.pet_cv.itemconfig(self.mascot, text=MASCOT.get(state, "🐹"))
        self._set_bubble(bubble, C[STATE_COLOR.get(state, "dim")])

    def _set_bubble(self, text, color=None):
        self.pet_cv.itemconfig(self.bubble, text=text, fill=color or C["dim"])

    def _refresh_idle_stats(self):
        st = latest_stats()
        if st["session"]:
            self.stats_lab.config(
                text=f"最近：{st['session'][:22]}\n"
                     f"击倒 {_or_dash(st['knockdowns'])} · "
                     f"拾取 {_or_dash(st['pickups'])} · "
                     f"语音 {_or_dash(st['voice'])}", fg=C["fg"])
        else:
            self.stats_lab.config(text="（还没有场次产物，跑一场就有了）")

    def _animate(self):
        t = time.time() - self._t0
        amp, period = (5.0, 0.9) if self.state == "run" else (1.6, 2.8)
        y = 42 - amp * abs(math.sin(2 * math.pi * t / period))
        self.pet_cv.coords(self.mascot, W // 2, y)
        self.after(33, self._animate)

    def _on_close(self):
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
