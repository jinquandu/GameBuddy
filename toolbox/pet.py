"""桌面宠物客户端（素朴版 v2）：极简小窗 + 系统托盘驻留。

python -m toolbox pet [--address <BV号/URL/场次目录>]

v2（2026-10-12 按用户反馈改素朴）：
- 主界面只留两样：录像地址（历史下拉 + 浏览）、「开始跑链」主按钮，
  外加一行状态文字（跑链进展/完成/出错提示——无时间线后的唯一反馈）。
- 标准窗口（系统标题栏），最小化即藏进右下角系统托盘（任务栏不占位），
  托盘菜单/双击图标恢复；依赖 pystray（未装则最小化退化为普通任务栏）。
- 桌面录制入口移除（CLI：python -m toolbox screenrec）；租户 ID 只在
  config.yaml 维护；✂自动剪辑/↻续跑等按钮能力并入主按钮状态机/CLI。

状态机：idle 待命 / run 跑链（n/9 + 当前阶段）/ done 完成 / err 出错
（主按钮变「↻ 续跑」，跳过已完成阶段）/ stop 手动停止。

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

from toolbox import proc
from toolbox.config import DATA, ROOT, load_config
from toolbox.logs import get as _get_log

LOG = _get_log("pet")
LOG_PIPE = _get_log("pipeline")       # 跑链子进程全量输出（排障用）

# ---------------------------------------------------------------- 常量

STAGES = [  # (阶段名, 显示名) —— 与 pipeline.run_pipeline 的 stage 顺序一致
    ("download", "下载"), ("asr", "ASR 转写"), ("speaker", "说话人区分"),
    ("detect", "OCR 行为检测"), ("knockdowns", "击倒切片"), ("pickups", "拾取切片"),
    ("queues", "匹配截图"), ("voice", "语音情绪评估"), ("push", "推评估包"),
]
# 进度权重（合计 1.0）：detect 是大头且与视频时长成正比，可精确到分片；
# knockdowns 按分片行计数；其余阶段开始即给阶段半程、完成给满程——
# 跳过的阶段权重自然计入已完成（核心包 asr/speaker/voice 常跳）。
STAGE_WEIGHT = {"download": .02, "asr": .03, "speaker": .01, "detect": .75,
                "knockdowns": .10, "pickups": .06, "queues": .01,
                "voice": .01, "push": .01}
STATE_COLOR = {"idle": "dim", "run": "accent", "done": "good",
               "err": "bad", "stop": "warn"}

C = {"bg": "#FFFEFC", "fg": "#3A3238", "dim": "#8C7F88", "accent": "#E8663C",
     "good": "#4CA37E", "warn": "#D99A3C", "bad": "#D9534F"}
FONT = "Microsoft YaHei UI"
W = 360                        # 小窗宽
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
    # 分片级进度：events.py 打印文件名截 36 字符 + 「：检测完成/复用事件流」；
    # knockdowns 每片一行「…：N 粗簇 -> …」
    _RE_DET_DONE = re.compile(r"^(.{1,64}?\.mp4)：(检测完成|复用事件流)")
    _RE_KD_SHARD = re.compile(r"^(.{1,64}?\.mp4)：\d+ 粗簇")

    def __init__(self):
        self.stages = {name: {"status": "pending", "secs": None}
                       for name, _ in STAGES}
        self.session = ""
        self.parts = 0
        self.counts = {"knockdowns": None, "pickups": None}
        self.detail = ""
        self.finished = False
        self.error_line = None
        # 进度模型（按时长）：媒体表 + 各阶段起点
        self.dur_by_prefix = {}
        self.total_dur = 0.0
        self.det_done_dur = 0.0
        self._det_counted = set()
        self.kd_seen = set()
        self.t0 = time.time()
        self._stage_t0 = {}

    def set_media(self, durations):
        """喂 {文件名: 秒}（跑链前 ffprobe 探得）；空表则退化为按片数。"""
        self.dur_by_prefix = {}
        self.total_dur = float(sum(durations.values()) or 0)
        for name, d in durations.items():
            self.dur_by_prefix[Path(name).name[:36]] = float(d)
        self.t0 = time.time()

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
                self._stage_t0[mm.group(1)] = time.time()
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
        mm = self._RE_DET_DONE.match(s)
        if mm:
            pre = mm.group(1)
            hit = False
            for k, d in self.dur_by_prefix.items():
                if pre.startswith(k):
                    hit = True
                    if k not in self._det_counted:
                        self._det_counted.add(k)
                        self.det_done_dur += d
                    break
            if not hit and pre not in self._det_counted:
                self._det_counted.add(pre)      # 无时长表：按行前缀计片数
            return
        mm = self._RE_KD_SHARD.match(s)
        if mm:
            pre = mm.group(1)
            for k in self.dur_by_prefix:
                if pre.startswith(k):
                    self.kd_seen.add(k)
                    break
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

    # ---------- 进度模型（按时长加权；detect 精确到分片时长比例）

    def progress(self):
        """-> (fraction 0..1, 当前阶段名)。跳过=完成；未开始阶段不加权。"""
        frac = sum(STAGE_WEIGHT[n] for n, st in self.stages.items()
                   if st["status"] in ("done", "skip"))
        cur = next((n for n, _ in STAGES
                    if self.stages[n]["status"] == "run"), None)
        if cur == "detect":
            if self.total_dur:
                frac += STAGE_WEIGHT["detect"] * min(
                    1.0, self.det_done_dur / self.total_dur)
            elif self.parts:                    # 无时长表：按片数退化
                frac += STAGE_WEIGHT["detect"] * min(
                    1.0, len(self._det_counted) / self.parts)
        elif cur == "knockdowns" and self.parts:
            frac += STAGE_WEIGHT["knockdowns"] * min(
                1.0, len(self.kd_seen) / self.parts)
        elif cur:
            frac += STAGE_WEIGHT[cur] * 0.5     # 无细粒度行：阶段中程
        if self.finished:
            return 1.0, cur
        return min(frac, 0.99), cur

    def eta_sec(self):
        """按整体已用时间与当前比例外推剩余秒；样本不足返回 None。"""
        frac, _ = self.progress()
        if self.finished:
            return 0
        if frac < 0.03:
            return None
        el = time.time() - self.t0
        return el * (1 - frac) / frac


# ---------------------------------------------------------------- 本地三线统计

def _clips_len(p: Path):
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if "shots" in d:                       # queues.json：只数已定位抽帧的局
        return len([s for s in d["shots"] if s.get("file")])
    return len(d.get("clips", []))


def latest_stats():
    """最近一场的三线计数：击倒/拾取取 mtime 最新的 JSON；语音按场次前缀分组合计。

    刚跑完的场次产物必然最新，空闲态展示它即「目前的进展」。
    """
    out = {"session": "", "knockdowns": None, "pickups": None, "voice": None,
           "queues": None}

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
    _, out["queues"] = newest("queues", "queues.json")

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


# ---------------------------------------------------------------- 素朴小窗

class PetWindow(tk.Tk):
    def __init__(self, address=""):
        super().__init__()
        self.title("陪玩大师姐 · 跑链")
        self.resizable(False, False)
        self.configure(bg=C["bg"])

        self.proc = None
        self.q = queue.Queue()
        self.parser = ProgressParser()
        self.state = "idle"          # idle/run/done/err/stop
        self.tray = None             # pystray.Icon（首次最小化时创建）
        self._logs = []              # 尾部日志（排障）
        self._status_text = ""
        self._status_sticky = False
        self._retry_skip = None       # 出错后续跑要跳过的已完成阶段
        self._last_addr = ""
        self._snap_queue = []
        hist = self._load_hist()
        self.run_eval_var = tk.BooleanVar(value=hist["run_eval"])
        self.watch_dir = hist.get("watch_dir", "")   # 后台日任务的目录兜底
        # 断链续跑（跨重启）：上次出错的场次+已完成阶段，启动即亮「↻ 续跑」
        self._retry_saved = {}
        r = hist.get("retry") or {}
        if r.get("addr") and r.get("skip") and Path(r["addr"]).exists():
            self._retry_saved = r
            self._retry_skip = list(r["skip"])
            self._last_addr = r["addr"]
        self.daily_stop = threading.Event()
        self.daily_thread = None

        self._build()
        if self._retry_saved:
            self.btn.config(text="↻  续跑")
        if address:
            self.addr_var.set(address)
        elif hist["addresses"]:
            self.addr_var.set(hist["addresses"][0])

        self._place_bottom_right()
        self.bind("<Unmap>", self._on_unmap)   # 最小化 → 托盘
        self._acct_refresh()
        self._daily_start()
        # 点 X 必须走 _on_close：Tk 默认销毁会绕过杀链/存历史/退托盘，
        # 跑链子进程成孤儿继续烧 CPU
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self.after(120, self._poll)
        self.after(2500, self._dev_snapshot)

    # ---------- 布局

    def _build(self):
        f = ttk.Frame(self, padding=(12, 10))
        f.pack(fill="both", expand=True)
        ttk.Label(f, text="录像地址（BV号 / URL / 场次目录）").pack(anchor="w")
        row = ttk.Frame(f)
        row.pack(fill="x", pady=(2, 8))
        self.addr_var = tk.StringVar()
        self.addr_cb = ttk.Combobox(row, textvariable=self.addr_var,
                                    values=self._load_hist()["addresses"])
        self.addr_cb.pack(side="left", fill="x", expand=True)
        ttk.Button(row, text="浏览…", width=7,
                   command=self._browse).pack(side="left", padx=(6, 0))
        self.btn = ttk.Button(f, text="▶  开始跑链",
                              command=self._on_main_button)
        self.btn.pack(fill="x", ipady=5, pady=(0, 6))
        # 进度条（按时长加权 + 剩余时间外推，仅 run 态显示）
        self.pbar = ttk.Progressbar(f, mode="determinate", maximum=100)
        self.plab = tk.Label(f, text="", bg=C["bg"], fg=C["dim"],
                             font=(FONT, 9), anchor="w")
        # 账号行（契约 §4A；服务未就绪时登录窗如实提示，不降级不绕过）
        acct = ttk.Frame(f)
        acct.pack(fill="x", pady=(0, 2))
        self.acct_lab = tk.Label(acct, text="账号：未登录", bg=C["bg"],
                                 fg=C["dim"], font=(FONT, 9), anchor="w")
        self.acct_lab.pack(side="left", fill="x", expand=True)
        self.acct_btn = ttk.Button(acct, text="登录", width=7,
                                   command=self._acct_click)
        self.acct_btn.pack(side="right")
        # 目录监控已退后台日任务（REQ-20261019-013）：不再有监控行，
        # 目录经 config folder_watch.watch_dir 配置，每日一轮自动跑链
        self.status_lab = tk.Label(f, text="", bg=C["bg"], fg=C["dim"],
                                   font=(FONT, 9), anchor="w", wraplength=W - 48,
                                   justify="left")
        self.status_lab.pack(fill="x")

    def _place_bottom_right(self):
        """首显落在屏幕右下（托盘附近），不打扰主工作区。"""
        self.update_idletasks()
        w, h = self.winfo_width(), self.winfo_height()
        sw, sh = self.winfo_screenwidth(), self.winfo_screenheight()
        self.geometry(f"+{sw - w - 28}+{sh - h - 88}")

    # ---------- 历史

    def _load_hist(self):
        try:
            d = json.loads(HIST.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            d = {}
        return {"addresses": d.get("addresses", []),
                "run_eval": bool(d.get("run_eval", False)),
                "watch_dir": d.get("watch_dir", ""),
                "retry": d.get("retry") or {}}   # 断链续跑：跨重启一键续

    def _save_hist(self):
        HIST.parent.mkdir(parents=True, exist_ok=True)
        addr = self.addr_var.get().strip()
        addrs = [a for a in self._load_hist()["addresses"] if a != addr]
        if addr:
            addrs.insert(0, addr)
        HIST.write_text(json.dumps(
            {"addresses": addrs[:8], "run_eval": bool(self.run_eval_var.get()),
             "watch_dir": self.watch_dir,
             "retry": getattr(self, "_retry_saved", None) or {}},
            ensure_ascii=False, indent=1), encoding="utf-8")

    # ---------- 账号（契约 §4A，C2S-10/11）

    def _acct_cfg(self):
        from toolbox.config import load_config
        a = load_config().get("account") or {}
        return a.get("api_url", ""), a.get("production_url") or None

    def _acct_click(self):
        from toolbox.account import AccountClient
        api, prod = self._acct_cfg()
        sess = AccountClient(api, prod).session_summary()
        if sess:
            AccountClient(api, prod).logout()
            self._acct_refresh()
            self._flash("已退出账号", C["dim"])
            return
        from toolbox.account_ui import login_dialog
        got = login_dialog(self, api, prod)
        self._acct_refresh()
        if got:
            self._flash(f"登录成功：{got.get('user_id', '?')}", C["accent"])

    def _acct_refresh(self):
        from toolbox.account import AccountClient
        api, prod = self._acct_cfg()
        try:
            sess = AccountClient(api, prod).session_summary()
        except Exception:                      # noqa: BLE001 状态行永不杀窗
            sess = {}
        if sess:
            self.acct_lab.config(
                text=f"账号：{sess.get('user_id', '?')}（{sess.get('phone', '')}）",
                fg=C["fg"])
            self.acct_btn.config(text="退出")
        else:
            self.acct_lab.config(text="账号：未登录", fg=C["dim"])
            self.acct_btn.config(text="登录")

    # ---------- 目录监控（后台日任务，REQ-20261019-013）
    #
    # UI 监控行已移除；pet 常驻进程内每日跑一轮「扫描入队→顺序跑完队列」，
    # 目录取 config folder_watch.watch_dir（历史 watch_dir 兜底迁移），
    # 无目录则任务不启动。错过当天时刻（daily_hour，默认 4 点）启动即补跑；
    # 当日已跑标记持久化在 .folder_watch.json（last_daily），一天恰好一轮。

    def _daily_start(self):
        if self.daily_thread and self.daily_thread.is_alive():
            return
        self.daily_stop.clear()
        self.daily_thread = threading.Thread(target=self._daily_loop,
                                             daemon=True)
        self.daily_thread.start()

    def _daily_loop(self):
        import datetime

        import toolbox.folder_watch as fw
        from toolbox.config import load_config
        cfg = load_config().get("folder_watch") or {}
        wdir = cfg.get("watch_dir") or self.watch_dir
        if not wdir:
            LOG.info("后台日任务未配置目录（folder_watch.watch_dir），不启动")
            return
        hour = int(cfg.get("daily_hour", 4))
        LOG.info("后台日任务就绪：%s（每日 %02d:00 一轮，错过启动补跑）",
                 wdir, hour)
        while not self.daily_stop.is_set():
            now = datetime.datetime.now()
            ran_today = fw.last_daily_run() == now.date().isoformat()
            if not ran_today and now.hour >= hour:
                LOG.info("后台日任务开始：%s", wdir)
                try:
                    n_ok, n_fail = self._daily_pass(wdir)
                    LOG.info("后台日任务完成：成功 %d / 失败 %d", n_ok, n_fail)
                    if n_ok or n_fail:
                        self.after(0, lambda ok=n_ok, fl=n_fail: self._flash(
                            f"后台处理完成：成{ok} 败{fl}（详见 pet.log）",
                            C["warn"] if fl else C["dim"]))
                except Exception as e:          # noqa: BLE001 单日失败不杀线程
                    LOG.warning("后台日任务失败：%s", e)
                fw.mark_daily_run()
            self.daily_stop.wait(timeout=600)   # 10 分钟粒度检查当日是否到期

    def _daily_pass(self, wdir):
        """跑完当前全部就绪任务（顺序，单日上限 100 保护）。"""
        import toolbox.folder_watch as fw
        n_ok = n_fail = 0
        for _ in range(100):
            new, results = fw.process_pending(wdir)
            if not results:
                break
            for session, ok, log in results:
                n_ok, n_fail = n_ok + ok, n_fail + (not ok)
                (LOG.info if ok else LOG.warning)(
                    "后台任务%s：%s%s", "成功" if ok else "失败", session,
                    "" if ok else f"（{log[-200:]}）")
        return n_ok, n_fail

    # ---------- 托盘（最小化驻留右下角通知区）

    def _on_unmap(self, _e):
        # wm_state() 而非 state()：self.state 是跑链状态属性，遮蔽了同名 Tk 方法
        if self.wm_state() == "iconic":         # 区分最小化与程序内 withdraw
            self.after(120, self._to_tray)

    def _to_tray(self):
        if self.wm_state() != "iconic":
            return
        self.withdraw()                          # 任务栏不再占位
        if self.tray is None:
            self._tray_init()
        if self.tray is not None:
            self._tray_tip(self._status_text or "待命")

    def _tray_init(self):
        try:
            import pystray
            from PIL import Image, ImageDraw
        except ImportError:
            LOG.info("未装 pystray，最小化停留在任务栏"
                     "（pip install pystray 可启用托盘驻留）")
            return
        img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
        d = ImageDraw.Draw(img)
        d.rounded_rectangle([4, 4, 60, 60], radius=14, fill="#E8663C")
        d.polygon([(26, 20), (26, 44), (48, 32)], fill="#FFFFFF")   # ▶
        menu = pystray.Menu(
            pystray.MenuItem("显示主界面", self._tray_act_show, default=True),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("退出", self._tray_act_quit),
        )
        self.tray = pystray.Icon("dashijie", img, "陪玩大师姐 · 跑链", menu)
        try:
            self.tray.run_detached()
        except Exception as e:                   # noqa: BLE001 托盘失败不杀主窗
            LOG.warning("托盘启动失败：%s", e)
            self.tray = None

    def _tray_act_show(self, *_):
        self.after(0, self._tray_show)           # pystray 线程 → UI 线程

    def _tray_act_quit(self, *_):
        self.after(0, self._on_close)

    def _tray_show(self):
        self.deiconify()
        self.wm_state("normal")
        self.lift()
        self._tray_stop()

    def _tray_stop(self):
        if self.tray is not None:
            t, self.tray = self.tray, None
            try:
                t.stop()
            except Exception:                    # noqa: BLE001 收尾尽力而为
                pass

    def _tray_tip(self, text):
        if self.tray is not None:
            try:
                self.tray.title = f"陪玩大师姐 · {text}"
            except Exception:                    # noqa: BLE001 提示更新尽力而为
                pass

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
        """主按钮分发：出错/带断链存档=续跑（--skip 已完成阶段），其余=开始/停止。"""
        if (self.state == "err" or
                (self.state in ("idle", "stop") and self._retry_saved)):
            addr = (self._last_addr or "").strip() or self.addr_var.get().strip()
            cur = self.addr_var.get().strip()
            # 跨重启续跑只在地址栏为空或与断链场次一致时生效；填了新址视为新跑
            if self._retry_skip and (self.state == "err" or not cur
                                     or cur == self._retry_saved.get("addr")
                                     or cur == addr):
                if addr:
                    self._start(addr, skip=self._retry_skip)
                    return
        self._toggle_run()

    def _probe_durations(self, addr):
        """本地目录/文件 -> {文件名: 秒}（ffprobe 逐个探测，失败容忍）。
        URL 场次返回空表，进度模型自动退化为按分片数。"""
        p = Path(addr)
        try:
            if p.is_file():
                vids = [p]
            elif p.is_dir():
                from toolbox.events import collect_videos
                vids = collect_videos(addr)
            else:
                return {}
            out = {}
            for v in vids:
                r = subprocess.run(
                    ["ffprobe", "-v", "error", "-show_entries",
                     "format=duration", "-of", "csv=p=0", str(v)],
                    capture_output=True, text=True, timeout=15,
                    creationflags=proc.NO_WINDOW)
                out[str(v)] = float(r.stdout.strip())
            LOG.info("进度时长表：%d 个视频共 %.0f 分钟",
                     len(out), sum(out.values()) / 60)
            return out
        except Exception as e:                   # noqa: BLE001 探测失败不拦跑链
            LOG.info("进度时长探测失败（退化为按片数）：%s", e)
            return {}

    def _start(self, addr, skip=()):
        # 登录门控（REQ-20261019-013）：未登录先弹登录框，成功则继续开跑
        try:
            from toolbox.account import require_login
            require_login()
        except Exception as e:                  # noqa: BLE001 门控拒绝即返回
            self._flash(str(e).split("] ", 1)[-1], C["warn"])
            api, prod = self._acct_cfg()
            if api:
                from toolbox.account_ui import login_dialog
                if login_dialog(self, api, prod):
                    self._acct_refresh()
                    self._flash("登录成功，继续…", C["accent"])
                else:
                    return
            else:
                return
        self._save_hist()
        self.parser = ProgressParser()
        self.parser.set_media(self._probe_durations(addr))
        self._logs = []
        self._retry_skip = None
        self._last_addr = addr
        self._hide_status()
        self._set_state("run", f"开跑：{Path(addr).name[:26]}")
        if getattr(sys, "frozen", False):         # exe 形态：自身即 CLI 入口
            args = [sys.executable, "pipeline", addr]
        else:
            args = [sys.executable, "-X", "utf8", "-m", "toolbox",
                    "pipeline", addr]
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
        self.btn.config(text="■  停止")

    def _reader(self):
        for line in self.proc.stdout:
            self.q.put(line.rstrip("\n"))
        self.q.put(None)            # EOF 哨兵

    def _kill_tree(self):
        if self.proc and self.proc.poll() is None:
            if os.name == "nt":     # pipeline 会再派生 ffmpeg/扫描子进程
                subprocess.run(["taskkill", "/PID", str(self.proc.pid),
                                "/T", "/F"], capture_output=True,
                               creationflags=proc.NO_WINDOW)
            else:
                self.proc.terminate()

    def _stop(self):
        self.state = "stop"         # 抢在 _finish 前定性，EOF 到达后不再覆盖
        self._kill_tree()
        self._set_state("stop", "已手动停止，素材我先收好…")
        self.btn.config(text="▶  开始跑链")

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
        if drained:
            self._render()
        self.after(120, self._poll)

    def _finish(self):
        if self.state == "stop":
            return
        code = self.proc.poll() if self.proc else 0
        if code is None:
            # stdout EOF 先于进程退出到达（收尾竞态）：此时判 None 会把
            # 明明跑完的链误报成失败（2026-10-09 pet.log rc=None 实录），
            # 等它真正退出再定性
            try:
                code = self.proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                code = -1
        if code == 0 and self.parser.finished:
            self._retry_saved = {}              # 跑通即清断链存档
            self._save_hist()
            st = latest_stats()
            name = (st["session"] or self.parser.session or "")[:18]
            self._set_state("done", f"完成！{name} 击倒{_or_dash(st['knockdowns'])} "
                                    f"拾取{_or_dash(st['pickups'])} "
                                    f"匹配{_or_dash(st['queues'])} "
                                    f"语音{_or_dash(st['voice'])}")
            LOG.info("跑链完成：%s", self._status_text)
        else:
            why = self.parser.error_line or f"退出码 {code}，看日志排查"
            self._set_state("err", f"出错：{why}")
            self._retry_skip = [name for name, _ in STAGES
                                if self.parser.stages[name]["status"]
                                in ("done", "skip")]
            self._retry_saved = {                 # 落盘：跨重启一键续跑
                "addr": self._last_addr or self.addr_var.get().strip(),
                "skip": self._retry_skip,
                "ts": time.strftime("%Y-%m-%d %H:%M:%S")}
            self._save_hist()
            LOG.warning("跑链失败：rc=%s err=%s 可续跑（跳过 %d 个已完成阶段）",
                        code, why, len(self._retry_skip))
        self.btn.config(text="↻  续跑" if self.state == "err"
                        else "▶  开始跑链")
        self._tray_tip("完成" if self.state == "done" else
                       ("出错，点续跑" if self.state == "err" else "待命"))
        if self.state == "done":
            self.pbar["value"] = 100
            self.after(4000, self._hide_progress)   # 满格停留一会儿再收
        else:
            self._hide_progress()

    def _hide_progress(self):
        self.pbar.pack_forget()
        self.plab.pack_forget()

    def _render(self):
        p = self.parser
        if self.state == "run":
            frac, cur = p.progress()
            eta = p.eta_sec()
            self.pbar.pack_forget()
            self.pbar.pack(fill="x", pady=(0, 2))
            self.plab.pack_forget()
            self.plab.pack(fill="x", pady=(0, 6))
            self.pbar["value"] = frac * 100
            bits = []
            disp = dict(STAGES).get(cur, "")
            if disp:
                bits.append(disp)
            bits.append(f"{int(frac * 100)}%")
            if eta is not None:
                bits.append(f"剩约{eta / 60:.0f}分" if eta >= 90
                            else "即将完成")
            el = (time.time() - p.t0) / 60
            if el >= 1:
                bits.append(f"已用{el:.0f}分")
            if p.session:
                bits.append(p.session[:14])
            text = " · ".join(bits)
            self._set_status(text, C["accent"])
            self._tray_tip(text)

    # ---------- 状态行（常驻一行文字；出错常驻，其余短闪）

    def _set_state(self, state, status):
        self.state = state
        if state == "err":
            self._show_status(status, C["bad"], sticky=True)
        elif state == "run":
            self._hide_status()                   # 进展由 _render 持续刷新
        else:                                      # idle/done/stop 短闪提示
            if status:
                self._flash(status, C[STATE_COLOR.get(state, "dim")])
            else:
                self._hide_status()

    def _show_status(self, text, color=None, sticky=False):
        self._status_text = text
        self._status_sticky = sticky
        self.status_lab.config(text=text, fg=color or C["dim"])

    def _hide_status(self):
        self._status_sticky = False
        self._status_text = ""
        self.status_lab.config(text="")

    def _set_status(self, text, color=None):
        """只改文字不改粘滞语义（run 态持续刷新用）。"""
        self._status_text = text
        self.status_lab.config(text=text, fg=color or C["dim"])

    def _flash(self, text, color=None, sticky=False):
        """状态行短闪（toast），2.5s 后自动收起；sticky=True 常驻到下次动作。"""
        self._show_status(text, color or C["accent"], sticky=sticky)
        if not sticky:
            self.after(2500, lambda: self._hide_status()
                       if self._status_text == text else None)

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
            for i, (n, _) in enumerate(STAGES):
                self.parser.stages[n] = {"status": "done", "secs": 60 + i * 37}
            self._set_state("done", "完成！击倒14 拾取9 语音6")

        self._snap_queue = [
            ("idle", lambda: self._set_state("idle", "")),
            ("run", demo_run),
            ("done", demo_done),
            ("err", lambda: self._set_state("err", "出错：OCR，看日志排查")),
        ]

        def step():
            if not self._snap_queue:
                return
            name, fn = self._snap_queue.pop(0)
            fn()
            self.after(300, lambda: grab(name) or step())

        step()

    def _on_close(self):
        # 后台日任务的跑链子进程同样要树杀（防孤儿烧 CPU）
        if self.daily_thread is not None and self.daily_thread.is_alive():
            import toolbox.folder_watch as fw
            self.daily_stop.set()
            fw.kill_current()
        if self.proc and self.proc.poll() is None:
            if not messagebox.askokcancel(
                    "还在跑", "识别链仍在运行，退出会终止整条链，确定？"):
                return
            self.state = "stop"
            self._kill_tree()
        self._save_hist()
        self._tray_stop()
        self.destroy()


def run(address=""):
    """GUI 入口（cli: python -m toolbox pet [--address ...]）。"""
    PetWindow(address=address).mainloop()
