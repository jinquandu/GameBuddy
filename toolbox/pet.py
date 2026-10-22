"""陪玩大师姐 PC 主窗（UI 对齐 REQ-20261021-003 范围2，2026-10-21）。

基线 = 15 页 UI V1 评审稿（cut_kb 交接/20261019-pc-ui-review）+ 全流程
Demo（用户拍板「UI 以全流程 Demo 为准」）；IDEA-006 新规则（微信前置、
扫描静默、三选一）按 REQ-20261021-001 定稿实现。信息架构 = 稿的
「工作台 / 我的作品 / 账号设置」左侧导航 + 白卡片；按用户 UI 偏好取
稿的配色语言（紫/粉）、不采立绘头像装饰。

前台无手动跑链 / BV / URL / 监控行（稿已确认边界）：生产由后台日任务
按规则驱动（每 2h 扫描入队 + 04:00/开机补跑触发 + 近 24h 新增>2GB，
滚动 24h 一组）；内部联调走 CLI `toolbox pipeline`。三选一在作品页
（通知服务 portal）完成，PC 端只做真实状态 + 入口（不以稿代验：
候选列表/发布接口契约后置 C2S-15 起另行起草）。

ProgressParser/STAGES 保留：CLI 跑链输出解析（progress_regress/
watch_pipeline_log 消费），主窗不再驱动手动跑链。
"""
import json
import os
import re
import sys
import threading
import time
import tkinter as tk
import webbrowser
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
    ("queues", "匹配截图"), ("voice", "语音情绪评估"), ("push", "推评估包"),
]
# 进度权重（合计 1.0）：detect 是大头且与视频时长成正比，可精确到分片；
# knockdowns 按分片行计数；其余阶段开始即给阶段半程、完成给满程——
# 跳过的阶段权重自然计入已完成（核心包 asr/speaker/voice 常跳）。
STAGE_WEIGHT = {"download": .02, "asr": .03, "speaker": .01, "detect": .75,
                "knockdowns": .10, "pickups": .06, "queues": .01,
                "voice": .01, "push": .01}

C = {"bg": "#FFFFFF", "fg": "#3A3238", "dim": "#8C7F88",
     "accent": "#7C5CE0", "good": "#4CA37E", "warn": "#D99A3C",
     "bad": "#D9534F"}
# 品牌配色语言（UI V1 评审稿）：页面浅紫灰底、白卡片、主紫/深紫/粉 CTA
BRAND = {"page": "#F2F1F6", "card": "#FFFFFF", "side": "#FFFFFF",
         "primary": "#7C5CE0", "deep": "#3B2A5E", "cta": "#F4517B",
         "lav": "#EFE9FB", "line": "#E7E4F0"}
FONT = "Microsoft YaHei UI"
W = 660                        # 主窗宽（左导航 136 + 内容区）
HIST = DATA / "pet" / "history.json"
LAST_WORKS = DATA / "pet" / "last_works.json"
NAV = (("home", "工作台"), ("works", "我的作品"), ("settings", "账号设置"))


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
            for k, d in self.dur_by_prefix.items():
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


# ---------------------------------------------------------------- 主窗

class PetWindow(tk.Tk):
    def __init__(self, address=""):
        super().__init__()
        self.title("陪玩大师姐")
        self.resizable(False, False)
        self.configure(bg=BRAND["page"])
        if address:
            LOG.info("pet --address 已忽略：前台无 BV/URL 入口（UI V1 评审稿）")

        hist = self._load_hist()
        self.watch_dir = hist.get("watch_dir", "")   # 后台日任务目录兜底
        self.tray = None             # pystray.Icon（首次最小化时创建）
        self.daily_stop = threading.Event()
        self.daily_thread = None
        self._status_text = ""
        self._status_sticky = False
        self._page = "home"
        self._pre = {}          # prerequisites() 最近快照（主线程渲染用）
        self._prod = {}         # 生产状态快照（summary/窗口/新增字节）
        self._sess = {}         # 当前登录会话快照
        self._env = {}          # 环境检测结果快照(REQ-20261022-005)
        self._env_busy = False  # 检测线程进行中(防重入)
        self._env_auto_user = None   # 本会话已自动检测过的账号(防重复跑)

        self._build()
        self.geometry(f"{W}x600")
        self._place_bottom_right()
        self.bind("<Unmap>", self._on_unmap)   # 最小化 → 托盘
        self._legacy_cleanup()
        self._acct_refresh()
        self._prod_refresh()
        self._daily_start()
        # 点 X 必须走 _on_close：绕过会跳过杀日任务子进程/存历史/退托盘
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self.after(15_000, self._queue_tick)   # 队列实时性(REQ-20261022-002
                                               # 优化:15s 自动刷新任务列表)
        self.after(2500, self._dev_snapshot)

    def _queue_tick(self):
        """队列实时刷新节拍(15s):新入队/状态变化自动上屏(用户反馈——
        加入新任务后列表滞后)。轻量:只读队列视图+渲染,不碰网络。"""
        self._prod_refresh()
        self.after(15_000, self._queue_tick)

    def _legacy_cleanup(self):
        """升级清理(REQ-20261022-005):旧版新手教程的素材缓存/沙盒/队列
        记录一并移除。缓存已被校验过的 demo 素材先迁给环境检测复用,省一次
        40MB 下载。全程尽力而为,失败不影响启动。"""
        try:
            from toolbox import envcheck
            envcheck.adopt_legacy_material()
            import toolbox.folder_watch as fw
            fw._purge_legacy_tutorial()
        except Exception as e:                  # noqa: BLE001 清理失败不拦启动
            LOG.info("旧版教程遗留清理跳过:%s", e)

    # ---------- 布局（左导航 + 三页卡片；素朴紧凑，配色=评审稿语言）

    def _card(self, parent, title):
        """白卡片：标题行 + 内容区。返回内容 Frame。"""
        card = tk.Frame(parent, bg=BRAND["card"], highlightbackground=
                        BRAND["line"], highlightthickness=1)
        card.pack(fill="x", padx=16, pady=(0, 10))
        tk.Label(card, text=title, bg=BRAND["card"], fg=BRAND["deep"],
                 font=(FONT, 10, "bold"), anchor="w").pack(
            fill="x", padx=12, pady=(10, 4))
        body = tk.Frame(card, bg=BRAND["card"])
        body.pack(fill="x", padx=12, pady=(0, 10))
        return body

    def _build(self):
        side = tk.Frame(self, bg=BRAND["side"], width=136)
        side.pack(side="left", fill="y")
        side.pack_propagate(False)
        tk.Label(side, text="陪玩大师姐", bg=BRAND["side"], fg=BRAND["deep"],
                 font=(FONT, 11, "bold")).pack(anchor="w", padx=14,
                                               pady=(14, 0))
        tk.Label(side, text="你打游戏 我剪高光", bg=BRAND["side"],
                 fg=C["dim"], font=(FONT, 8)).pack(anchor="w", padx=14)
        self._nav_btns = {}
        for key, txt in NAV:
            b = tk.Label(side, text=f" {txt}", anchor="w", cursor="hand2",
                         bg=BRAND["side"], fg=BRAND["deep"],
                         font=(FONT, 10), padx=10, pady=7)
            b.pack(fill="x", padx=8, pady=(10 if key == "home" else 2))
            b.bind("<Button-1>", lambda e, k=key: self._nav(k))
            self._nav_btns[key] = b
        # 侧栏底部：权益小卡（深紫）+ 账号行
        foot = tk.Frame(side, bg=BRAND["side"])
        foot.pack(side="bottom", fill="x", padx=8, pady=8)
        self.side_member = tk.Label(
            foot, text="权益 ·", bg=BRAND["deep"], fg="#FFFFFF",
            font=(FONT, 9), anchor="w", padx=8, pady=6, wraplength=104,
            justify="left")
        self.side_member.pack(fill="x")
        self.side_acct = tk.Label(foot, text="未登录", bg=BRAND["side"],
                                  fg=C["dim"], font=(FONT, 8), anchor="w")
        self.side_acct.pack(anchor="w", pady=(4, 0))

        body = tk.Frame(self, bg=BRAND["page"])
        body.pack(side="right", fill="both", expand=True)
        pages = tk.Frame(body, bg=BRAND["page"])
        pages.pack(fill="both", expand=True)
        self._pages = {}
        for key, _ in NAV:
            self._pages[key] = tk.Frame(pages, bg=BRAND["page"])

        # ---- 工作台：文件夹源 / 今日生产 / 后台队列
        home = self._pages["home"]
        b1 = self._card(home, "录像文件夹")
        self.dir_lab = tk.Label(b1, text="未选择", bg=BRAND["card"],
                                fg=C["fg"], font=(FONT, 10), anchor="w",
                                wraplength=360, justify="left")
        self.dir_lab.pack(side="left", fill="x", expand=True)
        ttk.Button(b1, text="更换文件夹", width=11,
                   command=self._browse).pack(side="right")
        tk.Label(home, text="每 2 小时自动检查一次新增录像，稳定后自动为你剪辑",
                 bg=BRAND["page"], fg=C["dim"], font=(FONT, 8),
                 anchor="w").pack(fill="x", padx=18)

        b2 = self._card(home, "今日生产")
        head2 = tk.Frame(b2, bg=BRAND["card"])
        head2.pack(fill="x")
        self.prod_badge = tk.Label(head2, text="…", bg=BRAND["lav"],
                                   fg=BRAND["primary"], font=(FONT, 9),
                                   padx=8, pady=2)
        self.prod_badge.pack(side="left")
        self.prod_act = ttk.Button(head2, text="", width=9)
        self.prod_act.pack(side="right")
        self.prod_detail = tk.Label(b2, text="", bg=BRAND["card"], fg=C["dim"],
                                    font=(FONT, 9), anchor="w", wraplength=440,
                                    justify="left")
        self.prod_detail.pack(fill="x", pady=(6, 0))

        b3 = self._card(home, "任务队列")
        head3 = tk.Frame(b3, bg=BRAND["card"])
        head3.pack(fill="x")
        self.queue_lab = tk.Label(head3, text="…", bg=BRAND["card"],
                                  fg=C["fg"], font=(FONT, 9), anchor="w")
        self.queue_lab.pack(side="left")
        ttk.Button(head3, text="刷新", width=6,
                   command=self._all_refresh).pack(side="right")
        # 任务列表(REQ-20261022-002 优化):每任务一行=状态徽标+场次名+
        # 删除钮;排序=入队顺序
        self.queue_rows = tk.Frame(b3, bg=BRAND["card"])
        self.queue_rows.pack(fill="x", pady=(4, 0))

        # ---- 我的作品：三选一说明 + 真实状态 + 作品页入口
        works = self._pages["works"]
        w1 = self._card(works, "每日三选一")
        self.works_lab = tk.Label(w1, text="…", bg=BRAND["card"], fg=C["fg"],
                                  font=(FONT, 9), anchor="w", wraplength=440,
                                  justify="left")
        self.works_lab.pack(side="left", fill="x", expand=True)
        ttk.Button(w1, text="打开作品页", width=11,
                   command=self._open_portal).pack(side="right")
        tk.Label(works, text="每天最多剪 3 条候选，挑 1 条最好的；选定后当天不能换，"
                             "发布前最后由你在抖音确认",
                 bg=BRAND["page"], fg=C["dim"], font=(FONT, 8), anchor="w",
                 wraplength=420, justify="left").pack(fill="x", padx=18)

        # ---- 账号设置：账号 / 生产前置 / 微信与通知 / 电脑与录像
        st = self._pages["settings"]
        s1 = self._card(st, "账号")
        self.set_acct_lab = tk.Label(s1, text="未登录", bg=BRAND["card"],
                                     fg=C["fg"], font=(FONT, 10), anchor="w")
        self.set_acct_lab.pack(side="left", fill="x", expand=True)
        self.acct_btn = ttk.Button(s1, text="登录", width=7,
                                   command=self._acct_click)
        self.acct_btn.pack(side="right")

        s2 = self._card(st, "开始剪辑的条件")
        pre_row = tk.Frame(s2, bg=BRAND["card"])
        pre_row.pack(fill="x")
        self.pre_labs = {}
        # 条件徽标与「登录/权益/微信」并列:第 4 项=环境检测
        # (REQ-20261022-005,替换原新手教程;登录后自动跑一次)
        for key, txt in (("login", "登录"), ("member", "权益"),
                         ("bind", "微信"), ("env", "环境")):
            lab = tk.Label(pre_row, text=txt, bg=BRAND["card"], fg=C["dim"],
                           font=(FONT, 9))
            lab.pack(side="left", padx=(0, 12))
            self.pre_labs[key] = lab
            if key == "env":
                lab.configure(cursor="hand2")
                lab.bind("<Button-1>", lambda e: self._env_click())
        btn_row = tk.Frame(pre_row, bg=BRAND["card"])
        btn_row.pack(side="right")
        self.bind_btn = ttk.Button(btn_row, text="去绑定", width=7,
                                   command=self._bind_click)
        self.bind_btn.pack(side="right")
        self.env_btn = ttk.Button(btn_row, text="检测", width=7,
                                  command=self._env_click)
        self.env_btn.pack(side="right", padx=(0, 6))
        self.env_detail = tk.Label(s2, text="", bg=BRAND["card"], fg=C["dim"],
                                   font=(FONT, 9), anchor="w",
                                   wraplength=440, justify="left")
        self.env_detail.pack(fill="x", pady=(6, 0))

        s3 = self._card(st, "微信与通知")
        self.set_bind_lab = tk.Label(s3, text="微信：未知", bg=BRAND["card"],
                                     fg=C["fg"], font=(FONT, 9), anchor="w")
        self.set_bind_lab.pack(side="left", fill="x", expand=True)
        self.opt_in_var = tk.BooleanVar(value=False)
        self._opt_busy = {"on": False}     # 程序置值不回发 consent
        self.opt_in_chk = ttk.Checkbutton(
            s3, text="成片通知", variable=self.opt_in_var,
            command=self._on_opt_in)
        self.opt_in_chk.pack(side="right")
        tk.Label(st, text="更换收片微信请联系大师姐；通知开关与绑定相互独立"
                          "（关通知≠解绑）",
                 bg=BRAND["page"], fg=C["dim"], font=(FONT, 8), anchor="w",
                 wraplength=420, justify="left").pack(fill="x", padx=18)

        s4 = self._card(st, "电脑与录像")
        from toolbox import device_identity as _di
        tk.Label(s4, text=f"本机设备 {_di.device_code()[:15]}…（一机一号，"
                          "换电脑需短信重新验证）",
                 bg=BRAND["card"], fg=C["fg"], font=(FONT, 9), anchor="w",
                 wraplength=440, justify="left").pack(fill="x")
        row4 = tk.Frame(s4, bg=BRAND["card"])
        row4.pack(fill="x", pady=(6, 0))
        self.set_dir_lab = tk.Label(row4, text="录像目录：未选择",
                                    bg=BRAND["card"], fg=C["fg"],
                                    font=(FONT, 9), anchor="w",
                                    wraplength=350, justify="left")
        self.set_dir_lab.pack(side="left", fill="x", expand=True)
        ttk.Button(row4, text="更换文件夹", width=11,
                   command=self._browse).pack(side="right")

        # ---- 常驻状态行（短闪提示；出错 sticky）
        self.status_lab = tk.Label(body, text="", bg=BRAND["page"],
                                   fg=C["dim"], font=(FONT, 9), anchor="w",
                                   wraplength=W - 180, justify="left")
        self.status_lab.pack(fill="x", padx=18, pady=(0, 8))
        self._nav("home")

    def _nav(self, page):
        self._page = page
        for key, _ in NAV:
            btn = self._nav_btns[key]
            sel = key == page
            btn.config(bg=BRAND["lav"] if sel else BRAND["side"],
                       fg=BRAND["primary"] if sel else BRAND["deep"])
            self._pages[key].pack_forget()
        self._pages[page].pack(fill="both", expand=True)

    def _place_bottom_right(self):
        """首显落在屏幕右下（托盘附近），不打扰主工作区。"""
        self.update_idletasks()
        w, h = self.winfo_width(), self.winfo_height()
        x = self.winfo_screenwidth() - w - 36
        y = self.winfo_screenheight() - h - 72
        self.geometry(f"+{max(x, 0)}+{max(y, 0)}")

    # ---------- 历史（目录持久化在 config，hist 只留兜底）

    def _load_hist(self):
        try:
            d = json.loads(HIST.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            d = {}
        return {"watch_dir": d.get("watch_dir", "")}

    def _save_hist(self):
        HIST.parent.mkdir(parents=True, exist_ok=True)
        HIST.write_text(json.dumps({"watch_dir": self.watch_dir},
                                   ensure_ascii=False, indent=1),
                        encoding="utf-8")

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
            if not messagebox.askokcancel("退出登录",
                                          "退出后今天就没办法自动为你剪辑了，确定退出？"):
                return
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
        except Exception:                      # noqa: BLE001 状态永不杀窗
            sess = {}
        self._sess = sess or {}
        if sess:
            self.set_acct_lab.config(
                text=f"{sess.get('user_id', '?')}（{sess.get('phone', '')}）",
                fg=C["fg"])
            self.acct_btn.config(text="退出")
            self.side_acct.config(text=str(sess.get('user_id', '?'))[:16],
                                  fg=C["fg"])
        else:
            self.set_acct_lab.config(text="未登录", fg=C["dim"])
            self.acct_btn.config(text="登录")
            self.side_acct.config(text="未登录", fg=C["dim"])
        self._env_refresh()
        if sess:
            self._env_auto()
        self._render_dir()
        self._prereq_refresh()

    # ---------- 三前置（REQ-20261021-001 规则1/方案A L1 体验层）

    def _prereq_refresh(self):
        """后台取数 → 主线程渲染；任何失败只收敛为「未知」，不杀窗。"""
        from toolbox.account import AccountClient
        api, prod = self._acct_cfg()
        if not api:
            self._pre = {}
            self.after(0, self._render_pre)
            return

        def run():
            pre = None
            try:
                pre = AccountClient(api, prod).prerequisites()
            except Exception:                  # noqa: BLE001
                pre = None
            self.after(0, lambda p=pre: self._render_pre(p or {}))
        threading.Thread(target=run, daemon=True).start()

    def _render_pre(self, pre=None):
        if pre is not None:
            self._pre = pre
        pre = self._pre
        mark = {True: ("✓", "good"), False: ("✗", "bad"),
                None: ("·", "dim")}
        binding = pre.get("binding")
        for key, val in (("login", pre.get("logged_in")),
                         ("member", pre.get("member")),
                         ("bind", {"bound": True, "none": False}.get(binding))):
            glyph, color = mark[val]
            name = {"login": "登录", "member": "权益", "bind": "微信"}[key]
            self.pre_labs[key].config(text=f"{name}{glyph}", fg=C[color])
        self._render_env()
        if binding == "none":                  # 仅明确未绑定才给入口
            self.bind_btn.pack(side="right")
        else:
            self.bind_btn.pack_forget()
        if binding == "bound":
            self.set_bind_lab.config(text="微信：已绑定", fg=C["good"])
        elif binding == "none":
            self.set_bind_lab.config(text="微信：未绑定", fg=C["bad"])
        else:
            self.set_bind_lab.config(text="微信：未知（服务未返回）",
                                     fg=C["dim"])
        # 侧栏权益卡
        m = pre.get("member")
        if m is True:
            exp = pre.get("member_expires")
            if exp:
                until = time.strftime("%m-%d", time.localtime(exp))
                self.side_member.config(text="权益 ✓\n至 " + until)
            else:
                self.side_member.config(text="权益 ✓")
        elif m is False:
            self.side_member.config(text="权益 未开通\n后台开通后生效")
        else:
            self.side_member.config(text="权益 ·")
        # 通知开关（程序置值不回发）
        opt = pre.get("opt_in")
        self._opt_busy["on"] = True
        try:
            self.opt_in_var.set(bool(opt))
            self.opt_in_chk.config(state="normal" if opt is not None
                                   and binding == "bound" else "disabled")
        finally:
            self._opt_busy["on"] = False
        self._render_prod()

    def _on_opt_in(self):
        if self._opt_busy["on"]:
            return
        api, prod = self._acct_cfg()
        val = self.opt_in_var.get()

        def run():
            from toolbox.account import AccountClient, AccountError
            try:
                AccountClient(api, prod).notification_consent(val)
                self.after(0, lambda: self._flash(
                    "成片通知已开启" if val else "成片通知已关闭（不影响绑定）",
                    C["accent"]))
            except AccountError as e:
                def back():
                    self._opt_busy["on"] = True
                    try:
                        self.opt_in_var.set(not val)
                    finally:
                        self._opt_busy["on"] = False
                    self._flash(f"通知设置失败：{e}", C["bad"])
                self.after(0, back)
        threading.Thread(target=run, daemon=True).start()

    def _bind_click(self):
        from toolbox.binding_ui import binding_dialog
        api, prod = self._acct_cfg()
        try:
            binding_dialog(self, api, prod)
        except Exception as e:                  # noqa: BLE001 绑定失败不杀主窗
            LOG.warning("绑定对话框异常：%s", e)
            self._flash("绑定流程异常，请稍后再试", C["warn"])
        self._acct_refresh()

    # ---------- 生产状态（工作台卡片；数据后台取）

    def _prod_refresh(self):
        """后台汇总 folder_watch 状态 → 主线程渲染（新增字节要走目录）。"""
        import toolbox.folder_watch as fw
        from toolbox.config import load_config

        def run():
            out = {}
            try:
                wdir = (load_config().get("folder_watch") or {}).get(
                    "watch_dir") or self.watch_dir
                out["wdir"] = wdir
                if wdir:
                    out["summary"] = fw.summary()
                    out["jobs"] = fw.jobs_view()   # 任务列表(状态/序)
                    out["last_ts"] = fw.last_production_ts()
                    out["due"] = fw.production_due(boot=False)
                    out["added"] = fw.new_recordings_bytes(wdir)
                    out["limit"] = fw.threshold_bytes()
            except Exception as e:              # noqa: BLE001 状态卡永不杀窗
                LOG.info("生产状态汇总失败：%s", e)
            self.after(0, lambda o=out: self._render_prod(o))
        threading.Thread(target=run, daemon=True).start()

    def _render_prod(self, prod=None):
        if prod is not None:
            self._prod = prod
        prod, pre = self._prod, self._pre
        q = prod.get("summary") or {}
        self.queue_lab.config(
            text=(f"排队 {q.get('queued', 0)} · 处理中 "
                  f"{q.get('running', 0)} · 完成 {q.get('done', 0)}"
                  + (f" · 失败 {len(q.get('failed', []))}"
                     if q.get("failed") else "")) if q else "还没有任务")
        self._render_queue_rows(prod.get("jobs") or [])
        self.works_lab.config(text=self._works_text(prod, q))
        # 工作台徽标链：目录 → 登录 → 绑定 → 权益 → 生产窗口
        api, _ = self._acct_cfg()
        badge, detail, act, cmd = "…", "", None, None
        if not prod.get("wdir"):
            badge, detail = "选择录像文件夹", "点「更换文件夹」选好目录，之后就全自动了"
        elif api and not pre.get("logged_in"):
            badge, detail = "未登录", "登录后就会自动为你剪辑（每天到点自动开始）"
            act, cmd = "去登录", self._acct_click
        elif pre.get("binding") == "none":
            badge, detail = "微信还没绑定", "绑定微信后今天就帮你补上"
            act, cmd = "去绑定", self._bind_click
        elif pre.get("member") is False:
            badge, detail = "还没有开通权益", "联系大师姐开通，开通后自动生效"
        elif q.get("running"):
            badge, detail = "剪辑中…", "正在处理你的录像，完成后自动出候选"
        elif prod.get("last_ts") and prod.get("due") and \
                not prod["due"][0] and "窗口内已生产" in prod["due"][1]:
            t = time.strftime("%H:%M", time.localtime(prod["last_ts"]))
            badge, detail = f"今天已出片 {t}", "候选在「我的作品」；明天还会继续"
            act, cmd = "看作品", lambda: self._nav("works")
        elif prod.get("added") is not None:
            gb = prod["added"] / 1e9
            hit = prod["added"] > prod.get("limit", 2e9)
            badge = "已达标，等自动剪辑" if hit else "录像还不够"
            detail = (f"最近一天新增录像 {gb:.2f}GB"
                      + ("，够了！到点自动开始剪辑" if hit
                         else "，再录一点吧（要超过 2GB 才开始剪）"))
        self.prod_badge.config(text=badge)
        color = {"未登录": C["bad"], "微信还没绑定": C["warn"],
                 "还没有开通权益": C["bad"], "剪辑中…": C["accent"],
                 "选择录像文件夹": C["dim"], "录像还不够": C["dim"]}.get(
                     badge, C["good"] if "已生产" in badge else BRAND["primary"])
        self.prod_badge.config(fg=color)
        self.prod_detail.config(text=detail)
        if act:
            self.prod_act.config(text=act, command=cmd)
            self.prod_act.pack()
        else:
            self.prod_act.pack_forget()
        self._render_dir()

    def _works_text(self, prod, q):
        """我的作品页状态行（真实数据；三选一在作品页完成）。文案面向
        客户——不带契约编号/内部术语（用户反馈 2026-10-21）。"""
        if not prod.get("wdir"):
            return ("先在工作台选好录像文件夹，之后每天会自动为你剪出候选。\n"
                    "每条候选都可以在这里打开作品页查看")
        if prod.get("last_ts"):
            t = time.strftime("%m-%d %H:%M", time.localtime(prod["last_ts"]))
            base = f"最近一次出片：{t}（已出 {q.get('done', 0)} 场）"
        else:
            base = "还没有作品。录像足够后（近 24 小时新增超过 2GB）会自动为你剪辑"
        return base + "\n每天最多剪出 3 条候选，点「打开作品页」去挑一条最好的"

    STATE_TXT = {"queued": "排队中", "running": "处理中", "done": "已完成"}

    def _render_queue_rows(self, jobs):
        """任务列表(REQ-20261022-002):状态徽标+场次名+删除钮,入队序。
        上限显示 8 行,余量以「…共 N 个」收尾;删除只移队列记录不动文件。"""
        for w in self.queue_rows.winfo_children():
            w.destroy()
        if not jobs:
            tk.Label(self.queue_rows, text="（空）", bg=BRAND["card"],
                     fg=C["dim"], font=(FONT, 8)).pack(anchor="w")
            return
        for i, j in enumerate(jobs[:8], 1):
            row = tk.Frame(self.queue_rows, bg=BRAND["card"])
            row.pack(fill="x")
            st = j["state"]
            stc = {"running": BRAND["primary"], "done": C["good"],
                   "failed": C["bad"]}.get(st, C["warn"])
            tk.Label(row, text=f"{i}.", bg=BRAND["card"], fg=C["dim"],
                     font=(FONT, 8), width=2).pack(side="left")
            tk.Label(row, text=self.STATE_TXT.get(st, st), bg=BRAND["card"],
                     fg=stc, font=(FONT, 8), width=5).pack(side="left")
            name = j["session"]
            tk.Label(row, text=name[:22] + ("…" if len(name) > 22 else ""),
                     bg=BRAND["card"], fg=C["fg"], font=(FONT, 9),
                     anchor="w").pack(side="left", fill="x", expand=True)
            ttk.Button(row, text="删除", width=4,
                       command=lambda k=j["key"]: self._queue_remove(k)
                       ).pack(side="right")
        if len(jobs) > 8:
            tk.Label(self.queue_rows, text=f"…共 {len(jobs)} 个任务",
                     bg=BRAND["card"], fg=C["dim"],
                     font=(FONT, 8)).pack(anchor="w")

    def _queue_remove(self, key):
        """删减任务(REQ-20261022-002):只移队列记录,不动磁盘录像。"""
        import toolbox.folder_watch as fw
        j = fw.remove(key)
        if j:
            self._flash(f"已移除任务:{j['session'][:20]}", C["dim"])
        self._prod_refresh()

    def _open_portal(self):
        """打开作品页:account.portal_url（显式配置）→ 最近一场真实成片的
        works_url（REQ-20261021-005 轮询期间落盘）→ 提示尚无作品。
        2026-10-22 用户要求:不再有 demo 视频发给用户看的环节。"""
        from toolbox.uploader import works_full_url
        cfg = load_config()
        url = ((cfg.get("account") or {}).get("portal_url") or "").strip()
        if not url:
            last = self._load_last_works()
            origin = ((cfg.get("server") or {}).get("upload_url")
                      or "").strip()
            url = works_full_url(origin, last.get("works_url") or "")
        if not url:
            self._flash("还没有作品可看：剪辑完成后会自动生成作品页", C["warn"])
            return
        webbrowser.open(url)

    def _load_last_works(self):
        try:
            d = json.loads(LAST_WORKS.read_text(encoding="utf-8"))
            return d if isinstance(d, dict) else {}
        except (OSError, ValueError):
            return {}

    def _save_last_works(self, session, works_url):
        """记住最近一场成片的作品页地址(打开作品页入口用)。"""
        if not works_url:
            return
        try:
            LAST_WORKS.parent.mkdir(parents=True, exist_ok=True)
            LAST_WORKS.write_text(json.dumps(
                {"session": session, "works_url": works_url,
                 "ts": time.time()}, ensure_ascii=False, indent=1),
                encoding="utf-8")
        except OSError as e:
            LOG.info("作品页地址落盘失败:%s", e)

    def _render_dir(self):
        # 内存值优先（用户刚选择=最新意图），config 兜底（启动时从配置来）
        wdir = self.watch_dir or (load_config().get("folder_watch") or {}).get(
            "watch_dir")
        short = str(wdir) if len(str(wdir)) <= 46 else str(wdir)[:44] + "…"
        self.dir_lab.config(text=short or "未选择",
                            fg=C["fg"] if wdir else C["dim"])
        self.set_dir_lab.config(text=f"录像目录：{short or '未选择'}")

    def _all_refresh(self):
        self._prereq_refresh()
        self._prod_refresh()

    # ---------- 目录唯一选择入口（IDEA-003/REQ-20261021-001）

    def _browse(self):
        d = filedialog.askdirectory(initialdir=str(ROOT / "video"),
                                    title="选择录像目录（后台每 2h 自动扫描）")
        if d:
            self._set_watch_dir(str(Path(d)))

    def _set_watch_dir(self, d):
        """所选目录即后台监控目录（唯一入口）：写内存+config 持久保存，
        日任务循环 10 分钟内接管（IDEA-003 交互细则）。"""
        if self.watch_dir == d:
            return
        self.watch_dir = d
        try:
            from toolbox.config import persist_watch_dir
            persist_watch_dir(d)
            LOG.info("监控目录已设置：%s", d)
        except Exception as e:                  # noqa: BLE001 持久化失败不拦选目录
            LOG.warning("watch_dir 持久化失败（本次运行仍生效）：%s", e)
        self._save_hist()
        self._render_dir()
        self._flash("录像目录已设置，后台每 2 小时自动扫描", C["accent"])
        self._prod_refresh()

    # ---------- 目录监控（后台日任务，REQ-20261019-013；
    #             扫描/生产分离 REQ-20261021-001 规则 2-4）
    #
    # 每 scan_interval_min（默认 120 分钟）扫描稳定入队（扫描≠生产）；
    # 生产触发 = 每日 daily_hour（默认 04:00）或开机补跑（24h 未生产即查，
    # 条件不豁免），滚动 24h 窗口内只触发一次（last_production_ts 持久化
    # 于 .folder_watch.json；服务端建组窗口 409 为最终权威）。触发门槛 =
    # 近 24h 新增录像严格 > threshold_gb（默认 2，十进制 GB，mtime 终值
    # 口径）；不足阈值不生产、当日权益照算（规则 5）。绑定门（方案A L1）
    # 未过 = 暂缓不记窗口，绑定完成后窗口内重试。

    def _daily_start(self):
        if self.daily_thread and self.daily_thread.is_alive():
            return
        self.daily_stop.clear()
        self.daily_thread = threading.Thread(target=self._daily_loop,
                                             daemon=True)
        self.daily_thread.start()

    def _daily_loop(self):
        import toolbox.folder_watch as fw
        from toolbox.config import load_config
        cfg = load_config().get("folder_watch") or {}
        hour = int(cfg.get("daily_hour", 4))
        scan_min = int(cfg.get("scan_interval_min", 120))
        LOG.info("后台日任务就绪（每 %d 分钟扫描入队；生产=每日 %02d:00 "
                 "或开机补跑，滚动 24h 一次，近 24h 新增>%.0fGB 触发；"
                 "未配置目录则等待选择）",
                 scan_min, hour, float(cfg.get("threshold_gb", 2)))
        boot = {"done": False}      # 开机补跑时刻只查一次（规则 4）
        next_scan = 0.0             # 首 tick 即扫（开机补扫停机期间稳定的）
        while not self.daily_stop.is_set():
            # 目录唯一入口（IDEA-003）可运行中改选：每拍重读，10 分钟内接管
            wdir = (load_config().get("folder_watch") or {}).get(
                "watch_dir") or self.watch_dir
            if wdir:
                # ① 扫描节拍（每 2h，稳定入队；扫描≠生产——规则 2）
                if time.time() >= next_scan:
                    try:
                        new = fw.scan(wdir)
                        if new:
                            LOG.info("扫描入队：%d 个新目标", new)
                    except Exception as e:          # noqa: BLE001 扫描失败不杀线程
                        LOG.warning("扫描失败：%s", e)
                    next_scan = time.time() + scan_min * 60
                # ② 生产判定（滚动 24h 窗口 + 阈值严格 >，规则 3/4/5）
                due, why = fw.production_due(hour, boot=not boot["done"])
                if due:
                    boot["done"] = True
                    # L1 预检（方案A §2）：明确未绑定 → 暂缓不记生产窗口，
                    # 绑定完成后窗口内自动重试（10 分钟粒度）
                    gate = self._bind_gate()
                    if gate:
                        LOG.warning("生产暂缓：%s", gate)
                        self.after(0, lambda g=gate: self._flash(
                            f"生产前置未满足：{gate}", C["warn"]))
                    else:
                        added = fw.new_recordings_bytes(wdir)
                        limit = fw.threshold_bytes()
                        if added > limit:           # 严格 >（等于/低于不启动）
                            LOG.info("生产开始（%s）：近 24h 新增 %.2fGB > "
                                     "%.0fGB", why, added / 1e9, limit / 1e9)
                            self.after(0, lambda: self._tray_tip("生产中"))
                            try:
                                n_ok, n_fail = self._daily_pass(wdir)
                                LOG.info("生产完成：成功 %d / 失败 %d",
                                         n_ok, n_fail)
                                if n_ok or n_fail:
                                    self.after(0, lambda ok=n_ok, fl=n_fail:
                                               self._flash(
                                                   f"生产完成：成{ok} 败{fl}"
                                                   "（详见 pet.log）",
                                                   C["warn"] if fl else C["dim"]))
                            except Exception as e:  # noqa: BLE001 单轮失败不杀线程
                                LOG.warning("生产失败：%s", e)
                        else:
                            # 规则 5：无新录像/不足阈值不生产，当日权益照算
                            LOG.info("生产跳过（%s）：近 24h 新增 %.2fGB ≤ "
                                     "%.0fGB，当日权益照算", why, added / 1e9,
                                     limit / 1e9)
                        fw.mark_production_run()   # 生产时刻消耗（含跳过）
                    self.after(0, self._prod_refresh)
            self.daily_stop.wait(timeout=600)   # 10 分钟粒度复查窗口/绑定门

    def _bind_gate(self):
        """L1 绑定预检：仅当「明确未绑定」（wechat-binding 返回 none）拦下；
        未知（服务端未返回/网络失败/未登录）不拦——登录门控由识别链路
        require_login 负责（REQ-20260919-013），权威判断在 C2S-12 服务端。
        返回暂缓原因文案或 ""。"""
        api, prod = self._acct_cfg()
        if not api:
            return ""                            # 内部机不门控
        from toolbox.account import AccountClient
        try:
            pre = AccountClient(api, prod).prerequisites()
        except Exception:                        # noqa: BLE001 预检失败放行
            return ""
        if pre.get("logged_in") and pre.get("binding") == "none":
            return "微信还没绑定（在「账号设置」里绑定后，今天就帮你补上）"
        return ""

    # ---------- 环境检测(REQ-20261022-005:替换新手教程)
    #
    # 登录后自动跑一次(每账号 7 天内不重复自动跑,手动「重新检测」随时可跑):
    # 系统/磁盘/视频工具实测 + 用 demo 素材真实解码与画面识别,结论落在
    # 「开始剪辑的条件」第四项,与登录/权益/微信并列。不发布任何作品、
    # 不打开作品页、不碰云端生产状态——只是回答「这台电脑能不能剪」。
    ENV_AUTO_DAYS = 7

    def _env_refresh(self):
        from toolbox import envcheck
        self._env = envcheck.last_result()
        self._render_env()

    def _env_result(self):
        """当前账号的检测结果(磁盘结果若属其他账号视为无结果)。"""
        uid = str((self._sess or {}).get("user_id") or "")
        r = self._env or {}
        if str(r.get("user_id") or uid) != uid:
            return {}
        return r

    def _render_env(self):
        """渲染环境徽标 + 一行摘要;未登录时显示「登录后自动检测」。
        结果按账号区分:磁盘上的结果若属其他账号(共用电脑换号),按未检测
        显示,不把上个账号的机器结论算到新账号头上。"""
        from toolbox import envcheck
        logged = bool(self._sess)
        res = self._env_result() if logged else {}
        state = envcheck.result_state(res) if logged else "off"
        glyph, color = {"off": ("·", C["dim"]), "pass": ("✓", C["good"]),
                        "warn": ("!", C["warn"]), "fail": ("✗", C["bad"])
                        }[state]
        self.pre_labs["env"].config(text=f"环境{glyph}", fg=color)
        if not logged:
            self.env_detail.config(
                text="登录后自动检测这台电脑能不能稳定剪片（约 1-2 分钟）",
                fg=C["dim"])
        elif not res:
            self.env_detail.config(
                text="本账号还没检测这台电脑，登录后会自动开始（约 1-2 分钟）",
                fg=C["dim"])
        else:
            self.env_detail.config(text=envcheck.summary_line(res),
                                   fg=C["dim"])
        self.env_btn.config(state="normal" if logged and not self._env_busy
                            else "disabled")

    def _env_auto(self):
        """登录后自动检测:同账号 7 天内的结果直接复用(不反复跑 OCR 基准),
        换账号或过期才重跑。正在剪片时让路(避免抢 CPU 拖慢识别),120 秒
        后再试。"""
        from toolbox import envcheck
        uid = str((self._sess or {}).get("user_id") or "")
        if not uid or self._env_busy:
            return
        r = self._env
        fresh = (str(r.get("user_id") or "") == uid
                 and time.time() - float(r.get("ts") or 0)
                 < self.ENV_AUTO_DAYS * 86400)
        if fresh or self._env_auto_user == uid:
            return
        if (self._prod.get("summary") or {}).get("running"):
            self.after(120_000, self._env_auto)
            return
        self._env_auto_user = uid
        self._env_run(manual=False)

    def _env_click(self):
        if not self._sess:
            self._flash("登录后会自动检测环境", C["warn"])
            return
        if self._env_busy:
            return
        if self._env_result():
            self._env_dialog()          # 有结果:先给详情,详情里可重新检测
        else:
            self._env_run(manual=True)

    def _env_run(self, manual=True):
        """后台跑完整检测;进度写「开始剪辑的条件」摘要行,结束刷新徽标。"""
        if self._env_busy:
            return
        self._env_busy = True
        self.env_btn.config(state="disabled")
        from toolbox import envcheck
        wdir = self.watch_dir or (load_config().get("folder_watch")
                                  or {}).get("watch_dir")
        uid = str((self._sess or {}).get("user_id") or "")
        t0 = time.time()
        self._flash("开始检测这台电脑…", C["accent"])

        def progress(frac, text):
            self.after(0, lambda t=text, f=frac: self.env_detail.config(
                text=f"{t}（{int(max(0.0, min(1.0, f)) * 100)}%）",
                fg=C["accent"]))

        def run():
            try:
                res = envcheck.run(progress=progress, watch_dir=wdir,
                                   user_id=uid)
            except Exception as e:              # noqa: BLE001 检测失败不杀窗
                LOG.warning("环境检测异常:%s", e)
                res = None
            self.after(0, lambda r=res: self._env_done(r, t0, manual))
        threading.Thread(target=run, daemon=True, name="envcheck").start()

    def _env_done(self, result, t0, manual):
        from toolbox import envcheck
        self._env_busy = False
        if not result:
            self._flash("环境检测出错了，稍后可再点「重新检测」", C["bad"],
                        sticky=True)
            self._render_env()
            return
        self._env = result
        self._render_env()
        cost = int(time.time() - t0)
        if result.get("incomplete"):
            self._flash("环境检测未完成（体验素材下载失败），"
                        "检查网络后点「重新检测」", C["warn"], sticky=True)
            self._tray_tip("环境检测未完成")
        elif result.get("ok"):
            head = ("环境检测通过" if not result.get("warn")
                    else f"环境可用（{result.get('warn')} 项提示）")
            self._flash(f"{head}，用时 {cost} 秒", C["good"], sticky=True)
            self._tray_tip("环境检测通过")
            if manual:
                self._env_dialog()
        else:
            bad = "、".join(i["label"] for i in result.get("items", [])
                            if i.get("level") == "bad")
            self._flash(f"环境检测发现问题：{bad}", C["bad"], sticky=True)
            self._tray_tip("环境检测异常")
            self._env_dialog()

    def _env_dialog(self):
        """检测详情(模态):六项结果 + 预计耗时 + 重新检测/关闭。"""
        from toolbox import envcheck
        r = self._env_result()
        win = tk.Toplevel(self)
        win.title("环境检测")
        win.resizable(False, False)
        win.configure(bg=BRAND["page"])
        win.transient(self)
        f = ttk.Frame(win, padding=(16, 12))
        f.pack(fill="both", expand=True)
        head = ("检测未完成" if r.get("incomplete")
                else "环境可用" if r.get("ok") else "这台电脑暂时不能剪片")
        color = (C["warn"] if r.get("incomplete")
                 else C["good"] if r.get("ok") else C["bad"])
        tk.Label(f, text=head, bg=BRAND["page"], fg=color,
                 font=(FONT, 12, "bold")).pack(anchor="w")
        if r.get("checked_at"):
            tk.Label(f, text=f"检测时间 {r['checked_at']}",
                     bg=BRAND["page"], fg=C["dim"], font=(FONT, 8)).pack(
                anchor="w")
        for icon, text in envcheck.detail_lines(r):
            row = tk.Frame(f, bg=BRAND["page"])
            row.pack(fill="x", pady=1)
            tk.Label(row, text=icon, bg=BRAND["page"],
                     fg={"✓": C["good"], "!": C["warn"], "✗": C["bad"]}.get(
                         icon, C["dim"]),
                     font=(FONT, 9, "bold"), width=2).pack(side="left")
            tk.Label(row, text=text, bg=BRAND["page"], fg=C["fg"],
                     font=(FONT, 9), anchor="w",
                     wraplength=400, justify="left").pack(side="left",
                                                         fill="x",
                                                         expand=True)
        est = (r.get("estimate") or {}).get("min_per_30")
        if est:
            tk.Label(f, text="说明：识别耗时按每秒 1 帧、多进程并行估算；"
                             "真实链路只识别画面局部，通常更快。",
                     bg=BRAND["page"], fg=C["dim"], font=(FONT, 8),
                     wraplength=420, justify="left").pack(anchor="w",
                                                          pady=(6, 0))
        if not r.get("ok"):
            tk.Label(f, text="可以先按上面的提示处理；处理不了就把这张单子"
                             "截图发给大师姐。",
                     bg=BRAND["page"], fg=C["warn"], font=(FONT, 8),
                     wraplength=420, justify="left").pack(anchor="w",
                                                          pady=(4, 0))
        row = ttk.Frame(f)
        row.pack(fill="x", pady=(10, 0))
        ttk.Button(row, text="重新检测", width=10,
                   command=lambda: (win.destroy(), self._env_run(True))
                   ).pack(side="left", padx=(0, 8))
        ttk.Button(row, text="关闭", width=8,
                   command=win.destroy).pack(side="left")
        win.wait_visibility()
        win.grab_set()

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
                if ok:
                    self._watch_produce(session)   # 成片轮询（REQ-005）
        return n_ok, n_fail

    def _watch_produce(self, session):
        """每场跑链成功后跟踪云端成片（REQ-20261021-005）：后台线程轮询
        produce 状态（60s/次，上限 1h），done → flash+托盘+状态行引导打开
        作品页；failed → 展示原因。仅账号模式（usr_* 自动排队）有意义；
        内部机/无 upload_id 静默跳过。"""
        import toolbox.folder_watch as fw
        from toolbox import uploader as _up
        from toolbox.config import load_config
        cfg = (load_config().get("server") or {})
        if not cfg.get("upload_url") or cfg.get("upload_token"):
            return                                  # 仅账号模式
        try:
            from toolbox.transfer import discover_session
            disc = discover_session(session)
            files = []                              # 仅取签名：重算一次清单
            # 复用 push 登记的最近 upload_id（同 session+eval）
            reg = {}
            rp = _up._registry_path()
            if rp.is_file():
                reg = json.loads(rp.read_text(encoding="utf-8"))
            sig = None
            for (s, m, signature), uid in (reg or {}).items():
                if s == disc["session"] and m == "eval":
                    sig = (s, m, signature)
                    break
            upload_id = reg.get(sig) if sig else None
            if not upload_id:
                return
        except Exception as e:                      # noqa: BLE001 探测失败跳过
            LOG.info("成片跟踪跳过（%s 场）：%s", session, e)
            return
        api_prod = cfg["upload_url"]

        def poll():
            up = _up.ProductionUploader(api_prod, token=None)
            # 账号模式需要签名;用 require_login 的会话
            try:
                from toolbox.account import require_login
                from toolbox import device_identity as di
                _s = require_login()
                _b = _s["bearer"]
                up2 = _up.ProductionUploader(
                    api_prod, token=_b,
                    signer=lambda m, w, d: di.sign_request(m, w, d, _b))
            except Exception as e:                  # noqa: BLE001
                LOG.info("成片跟踪停止（未登录）：%s", e)
                return

            def on_state(s):
                LOG.info("成片状态（%s）：%s", session,
                         (s or {}).get("state"))

            result = up2.wait_produce(upload_id, on_state=on_state)
            state = (result or {}).get("state")
            if state == "done":
                from toolbox.uploader import works_full_url
                works = (result or {}).get("works_url", "")
                url = works_full_url(api_prod, works)
                self._save_last_works(session, works)
                msg = "成片完成！3 条候选已就绪"
                LOG.info("%s（%s）", msg, url)
                self.after(0, lambda: (self._flash(msg + "，点「我的作品」"
                                                   " →「打开作品页」去挑",
                                                   C["accent"], sticky=True),
                                       self._tray_tip("成片完成")))
            elif state == "failed":
                err = (result or {}).get("error") or ""
                LOG.warning("成片生成失败（%s）：%s", session, err[-300:])
                self.after(0, lambda: self._flash(
                    "成片生成失败，请重试上传（原始录像不受影响）", C["bad"]))

        threading.Thread(target=poll, daemon=True,
                         name=f"produce-{session[:12]}").start()

    # ---------- 托盘（最小化驻留右下角通知区）

    def _on_unmap(self, _e):
        # wm_state() 而非 state()：避免与任何同名属性遮蔽 Tk 方法
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
        d.rounded_rectangle([4, 4, 60, 60], radius=14, fill=BRAND["primary"])
        d.polygon([(26, 20), (26, 44), (48, 32)], fill="#FFFFFF")   # ▶
        menu = pystray.Menu(
            pystray.MenuItem("显示主界面", self._tray_act_show, default=True),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("退出", self._tray_act_quit),
        )
        self.tray = pystray.Icon("dashijie", img, "陪玩大师姐", menu)
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

    # ---------- 状态行（常驻一行文字；出错常驻，其余短闪）

    def _show_status(self, text, color=None, sticky=False):
        self._status_text = text
        self._status_sticky = sticky
        self.status_lab.config(text=text, fg=color or C["dim"])

    def _hide_status(self):
        self._status_sticky = False
        self._status_text = ""
        self.status_lab.config(text="")

    def _flash(self, text, color=None, sticky=False):
        """状态行短闪（toast），2.5s 后自动收起；sticky=True 常驻到下次动作。"""
        self._show_status(text, color or C["accent"], sticky=sticky)
        if not sticky:
            self.after(2500, lambda: self._hide_status()
                       if self._status_text == text else None)

    # ---------- 调试快照（PET_SNAPSHOT=<目录> 时各页截屏）

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

        self._snap_queue = [
            ("home", lambda: self._nav("home")),
            ("works", lambda: self._nav("works")),
            ("settings", lambda: self._nav("settings")),
        ]

        def step():
            if not self._snap_queue:
                return
            name, fn = self._snap_queue.pop(0)
            fn()
            self.after(300, lambda: grab(name) or step())

        step()

    def _on_close(self):
        # 后台日任务的跑链子进程树杀（防孤儿烧 CPU）
        if self.daily_thread is not None and self.daily_thread.is_alive():
            import toolbox.folder_watch as fw
            self.daily_stop.set()
            fw.kill_current()
        self._save_hist()
        self._tray_stop()
        self.destroy()


def run(address=""):
    """GUI 入口（cli: python -m toolbox pet）。address 已忽略（UI V1 无
    BV/URL 入口，内部联调走 toolbox pipeline）。"""
    PetWindow(address=address).mainloop()
