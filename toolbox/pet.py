"""陪玩大师姐 PC 主窗（UI 对齐 REQ-20261021-003 范围2，2026-10-21）。

基线 = 15 页 UI V1 评审稿（cut_kb 交接/20261019-pc-ui-review）+ 全流程
Demo（用户拍板「UI 以全流程 Demo 为准」）；IDEA-006 新规则（微信前置、
扫描静默、三选一）按 REQ-20261021-001 定稿实现。信息架构 = 稿的
「工作台 / 我的作品 / 账号设置」左侧导航 + 白卡片；按用户 UI 偏好取
稿的配色语言（紫/粉）、不采立绘头像装饰。

前台无手动跑链 / BV / URL / 监控行（稿已确认边界）：生产由后台日任务
按规则驱动（每 2h 扫描入队 + 04:00/开机补跑触发 + 未处理录像>2GB，
滚动 24h 一组）；内部联调走 CLI `toolbox pipeline`。三选一在作品页
（通知服务 portal）完成，PC 端只做真实状态 + 入口（不以稿代验：
候选列表/发布接口契约后置 C2S-15 起另行起草）。

ProgressParser/STAGES 保留：CLI 跑链输出解析（progress_regress/
watch_pipeline_log 消费），主窗不再驱动手动跑链。
"""
import json
import os
import sys
import re
import threading
import time
import tkinter as tk
import webbrowser
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

from toolbox import dpi, ui_kit, version
from toolbox.config import DATA, ROOT, load_config
from toolbox.logs import get as _get_log
from toolbox.ui_kit import (BRAND, F_BODY, F_DIALOG, F_H1, F_SMALL, F_TITLE,
                            FONT)

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

# 颜色/字体/间距见 toolbox/ui_kit(REQ-20261022-007 统一组件规范);
# BRAND/F_* 由 ui_kit 导入,这里只保留窗口级常量。
W = 660                        # 主窗逻辑宽(左导航 136 + 内容区)
HIST = DATA / "pet" / "history.json"
LAST_WORKS = DATA / "pet" / "last_works.json"


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


def _eta_text(minutes):
    """分钟 → 客户可读（<1 分钟 / 约 X 分钟 / 约 X 小时Y分）。"""
    m = int(float(minutes) + 0.5)
    if m <= 0:
        return "<1 分钟"
    if m < 60:
        return f"约 {m} 分钟"
    return f"约 {m // 60} 小时{m % 60}分" if m % 60 else f"约 {m // 60} 小时"


def _abs_url(info):
    """清单里的包 url(可能相对)→ 绝对地址(按清单候选 origin 拼)。"""
    import toolbox.auto_update as au
    return au._abs(info.get("pkg_url") or "", au.manifest_urls())


class PetWindow(tk.Tk):
    """主窗(REQ-20261022-006/007/008/010 改版)。

    布局:左导航(图标+文字) / 内容区(可滚动) / 底部状态行。
    状态可信原则:只显示可核实结果——本地队列完成 ≠ 云端出片;云端
    produce 未完成时显示「云端生成中」或「结果待确认」,不提前报成片。
    """

    ENV_AUTO_DAYS = 7
    STATE_TXT = {"queued": "排队中", "running": "处理中", "done": "已完成",
                 "failed": "失败"}

    def __init__(self, address=""):
        dpi.setup()                       # 必须在建任何窗口之前(REQ-006)
        super().__init__()
        dpi.apply_tk(self)
        self.title(version.PRODUCT)
        self.configure(bg=BRAND["page"])
        self.minsize(dpi.px(600), dpi.px(520))
        self.resizable(True, True)
        ui_kit.setup_style(self)
        self._apply_window_icon()
        if address:
            LOG.info("pet --address 已忽略：前台无 BV/URL 入口（UI V1 评审稿）")

        hist = self._load_hist()
        self.watch_dir = hist.get("watch_dir", "")
        self.tray = None
        self.daily_stop = threading.Event()
        self.daily_thread = None
        self._rec = None            # 桌面录制器（None=未在录）
        self._rec_stopping = False
        self._rec_auto_on = False   # 自动录制开关(游戏启停联动)
        self._auto_wake = threading.Event()   # 开关立即响应(免等轮询拍)
        self._status_text = ""
        self._status_sticky = False
        self._page_key = "home"
        self._pre = {}
        self._prod = {}
        self._sess = {}
        self._env = {}
        self._env_busy = False
        self._env_auto_user = None
        self._auth_notified = False   # 死会话提示只闪一次
        self._produce = {}          # session -> {state,error,works_url,ts}
        self._last_works = self._load_last_works()

        self._build()
        self.geometry(f"{dpi.px(W)}x{dpi.px(600)}")
        self._place_bottom_right()
        self.bind("<Unmap>", self._on_unmap)
        hist = self._load_hist()
        if hist.get("auto_record"):
            self.rec_auto_var.set(True)
            self._rec_auto_on = True
            self._rec_render()
            self.after(600, self._rec_watch_start)
        self._legacy_cleanup()
        self._acct_refresh()
        self._prod_refresh()
        self._daily_start()
        self._rules_start()               # 规则定时对齐(REQ-20261026-001)
        self._upd_phase = "idle"          # idle/checking/downloading/
                                          # deferred/ready/none/disabled
        self._upd_info = {}               # 清单摘要(版本/说明/required)
        self._update_restore_ready()      # 上次已就绪的重启后仍可应用
        self._update_start()              # 自动更新(REQ-20261026-002)
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self.after(15_000, self._queue_tick)
        self.after(2500, self._dev_snapshot)

    def _apply_window_icon(self):
        """窗口/任务栏图标(REQ-007 V08):用包内头像生成 ico,失败不影响启动。"""
        try:
            base = Path(__file__).resolve().parent / "pet_assets"
            png = base / "avatar.png"
            if not png.is_file():
                return
            from PIL import Image, ImageTk
            img = Image.open(png).convert("RGBA")
            self._icon_photo = ImageTk.PhotoImage(img)
            self.iconphoto(True, self._icon_photo)
        except Exception as e:                  # noqa: BLE001
            LOG.info("窗口图标设置跳过:%s", e)

    def _queue_tick(self):
        """队列实时刷新节拍(15s):新入队/状态变化自动上屏。轻量:只读队列
        视图+渲染,不碰网络;诊断/进度也随这一拍刷新。"""
        self._prod_refresh()
        self.after(15_000, self._queue_tick)

    def _legacy_cleanup(self):
        """升级清理(REQ-20261022-005):旧版新手教程的素材缓存/沙盒/队列
        记录一并移除。校验过的 demo 素材先迁给环境检测复用,省一次下载。"""
        try:
            from toolbox import envcheck
            envcheck.adopt_legacy_material()
            import toolbox.folder_watch as fw
            fw._purge_legacy_tutorial()
        except Exception as e:                  # noqa: BLE001 清理失败不拦启动
            LOG.info("旧版教程遗留清理跳过:%s", e)

    # ---------- 布局 ----------

    def _brand_mark(self, parent, bg):
        c = tk.Canvas(parent, width=dpi.px(22), height=dpi.px(22), bg=bg,
                      bd=0, highlightthickness=0, takefocus=0)
        c.create_oval(dpi.px(1), dpi.px(1), dpi.px(21), dpi.px(21),
                      fill=BRAND["primary"], outline="")
        c.create_polygon(dpi.px(9), dpi.px(6), dpi.px(9), dpi.px(16),
                         dpi.px(16), dpi.px(11), fill="#FFFFFF", outline="")
        return c

    def _build(self):
        side = tk.Frame(self, bg=BRAND["side"], width=dpi.px(136))
        side.pack(side="left", fill="y")
        side.pack_propagate(False)
        head = tk.Frame(side, bg=BRAND["side"])
        head.pack(fill="x", padx=dpi.px(12), pady=(dpi.px(14), 0))
        self._brand_mark(head, BRAND["side"]).pack(side="left")
        tk.Label(head, text=version.PRODUCT, bg=BRAND["side"],
                 fg=BRAND["deep"], font=F_H1).pack(side="left",
                                                   padx=(dpi.px(6), 0))
        tk.Label(side, text="你打游戏 我剪高光", bg=BRAND["side"],
                 fg=BRAND["dim"], font=F_SMALL).pack(anchor="w",
                                                     padx=dpi.px(14))

        self._nav_btns = {}
        self._nav_icons = {}
        for key, txt, ic in (("home", "工作台", "home"),
                             ("works", "我的作品", "works"),
                             ("settings", "账号设置", "settings")):
            row = tk.Frame(side, bg=BRAND["side"], cursor="hand2")
            row.pack(fill="x", padx=dpi.px(8),
                     pady=(dpi.px(8) if key == "home" else dpi.px(2), 0))
            icon_c = ui_kit.icon(row, ic, BRAND["deep"], BRAND["side"])
            icon_c.pack(side="left", padx=(dpi.px(8), dpi.px(6)),
                        pady=dpi.px(6))
            lab = tk.Label(row, text=txt, anchor="w", bg=BRAND["side"],
                           fg=BRAND["deep"], font=F_BODY)
            lab.pack(side="left", fill="x", expand=True, pady=dpi.px(6))
            for w in (row, icon_c, lab):
                w.bind("<Button-1>", lambda e, k=key: self._nav(k))
            self._nav_btns[key] = row
            self._nav_icons[key] = (icon_c, lab)

        foot = tk.Frame(side, bg=BRAND["side"])
        foot.pack(side="bottom", fill="x", padx=dpi.px(8), pady=dpi.px(8))
        self.side_member = tk.Label(
            foot, text="权益 ·", bg=BRAND["deep"], fg="#FFFFFF", font=F_SMALL,
            anchor="w", padx=dpi.px(8), pady=dpi.px(6),
            wraplength=dpi.px(104), justify="left")
        self.side_member.pack(fill="x")
        self.side_acct = tk.Label(foot, text="未登录", bg=BRAND["side"],
                                  fg=BRAND["dim"], font=F_SMALL, anchor="w",
                                  wraplength=dpi.px(116), justify="left")
        self.side_acct.pack(anchor="w", pady=(dpi.px(4), 0))
        tk.Label(foot, text=version.display(),
                 bg=BRAND["side"], fg=BRAND["faint"],
                 font=(FONT, 7)).pack(anchor="w", pady=(dpi.px(2), 0))

        body = tk.Frame(self, bg=BRAND["page"])
        body.pack(side="right", fill="both", expand=True)
        self.status_lab = tk.Label(body, text="", bg=BRAND["page"],
                                   fg=BRAND["dim"], font=F_SMALL, anchor="w",
                                   wraplength=dpi.px(430), justify="left")
        self.status_lab.pack(side="bottom", fill="x", padx=dpi.px(16),
                             pady=(0, dpi.px(6)))

        self._pages = {}
        for key in ("home", "works", "settings"):
            self._pages[key] = ui_kit.ScrollFrame(body, BRAND["page"])

        self._build_home(self._pages["home"].inner)
        self._build_works(self._pages["works"].inner)
        self._build_settings(self._pages["settings"].inner)
        self._nav("home")
        self._rec_render()

    def _build_home(self, home):
        b1 = ui_kit.card(home, "录像文件夹")
        self.dir_row = tk.Frame(b1, bg=BRAND["card"])
        self.dir_row.pack(fill="x")
        ui_kit.icon(self.dir_row, "folder", BRAND["primary"],
                    BRAND["card"]).pack(side="left", padx=(0, dpi.px(6)))
        self.dir_lab = tk.Label(self.dir_row, text="未选择", bg=BRAND["card"],
                                fg=BRAND["fg"], font=F_BODY, anchor="w",
                                wraplength=dpi.px(330), justify="left")
        self.dir_lab.pack(side="left", fill="x", expand=True)
        ui_kit.button(self.dir_row, "更换文件夹", self._browse,
                      kind="brand").pack(side="right")
        self.dir_note = tk.Label(b1, text="", bg=BRAND["card"],
                                 fg=BRAND["dim"], font=F_SMALL, anchor="w",
                                 wraplength=dpi.px(420), justify="left")
        self.dir_note.pack(fill="x", pady=(dpi.px(6), 0))

        br = ui_kit.card(home, "桌面录制")
        rec_row = tk.Frame(br, bg=BRAND["card"])
        rec_row.pack(fill="x")
        self.rec_lab = tk.Label(rec_row, text="未在录制", bg=BRAND["card"],
                                fg=BRAND["fg"], font=F_BODY, anchor="w",
                                wraplength=dpi.px(230), justify="left")
        self.rec_lab.pack(side="left", fill="x", expand=True)
        self.rec_pause_btn = ui_kit.button(rec_row, "暂停", self._rec_pause,
                                           kind="ghost")
        # 用户指示(2026-10-26):取消「开始录制」手动钮,改「自动录制」开关
        # ——开着就监控三角洲行动:游戏启动自动录,退出自动收尾排队
        self.rec_auto_var = tk.BooleanVar(value=False)
        self.rec_auto_chk = ui_kit.checkbutton(
            rec_row, "自动录制", self.rec_auto_var,
            command=self._rec_auto_toggle)
        self.rec_auto_chk.pack(side="right")
        self.rec_note = tk.Label(br,
                                 text="开着自动录制：进三角洲行动就自动录，"
                                      "退游戏自动收尾，录完整个文件夹自动"
                                      "排队剪辑",
                                 bg=BRAND["card"], fg=BRAND["dim"],
                                 font=F_SMALL, anchor="w",
                                 wraplength=dpi.px(430), justify="left")
        self.rec_note.pack(fill="x", pady=(dpi.px(6), 0))

        b2 = ui_kit.card(home, "今日进度")
        head2 = tk.Frame(b2, bg=BRAND["card"])
        head2.pack(fill="x")
        self.prod_badge = tk.Label(head2, text="…", bg=BRAND["lav"],
                                   fg=BRAND["primary"], font=F_BODY,
                                   padx=dpi.px(8), pady=dpi.px(3))
        self.prod_badge.pack(side="left")
        self.prod_act = ui_kit.button(head2, "", kind="small")
        self.prod_act.pack(side="right")
        self.prod_detail = tk.Label(b2, text="", bg=BRAND["card"],
                                    fg=BRAND["dim"], font=F_SMALL, anchor="w",
                                    wraplength=dpi.px(430), justify="left")
        self.prod_detail.pack(fill="x", pady=(dpi.px(6), 0))

        b3 = ui_kit.card(home, "任务队列")
        head3 = tk.Frame(b3, bg=BRAND["card"])
        head3.pack(fill="x")
        self.queue_lab = tk.Label(head3, text="…", bg=BRAND["card"],
                                  fg=BRAND["fg"], font=F_BODY, anchor="w")
        self.queue_lab.pack(side="left")
        ui_kit.button(head3, "刷新", self._all_refresh,
                      kind="ghost").pack(side="right")
        self.queue_rows = tk.Frame(b3, bg=BRAND["card"])
        self.queue_rows.pack(fill="x", pady=(dpi.px(4), 0))

    def _build_works(self, works):
        w1 = ui_kit.card(works, "我的作品")
        self.works_row = tk.Frame(w1, bg=BRAND["card"])
        self.works_row.pack(fill="x")
        self.works_lab = tk.Label(self.works_row, text="…", bg=BRAND["card"],
                                  fg=BRAND["fg"], font=F_BODY, anchor="w",
                                  wraplength=dpi.px(340), justify="left")
        self.works_lab.pack(side="left", fill="x", expand=True)
        self.works_btn = ui_kit.button(self.works_row, "打开作品页",
                                       self._open_portal, kind="brand")
        self.works_btn.pack(side="right")
        self.works_empty = tk.Frame(w1, bg=BRAND["card"])
        ui_kit.hint(works,
                    "每天最多剪 3 条候选，挑 1 条最好的。" + "\n" +
                    "选片与发布以作品页当前提示为准，最终由你在抖音确认。",
                    wraplength=520)

    def _build_settings(self, st):
        s1 = ui_kit.card(st, "账号")
        row1 = tk.Frame(s1, bg=BRAND["card"])
        row1.pack(fill="x")
        self.set_acct_lab = tk.Label(row1, text="未登录", bg=BRAND["card"],
                                     fg=BRAND["fg"], font=F_BODY, anchor="w")
        self.set_acct_lab.pack(side="left", fill="x", expand=True)
        self.acct_btn = ui_kit.button(row1, "登录", self._acct_click,
                                      kind="brand", width=7)
        self.acct_btn.pack(side="right")
        row1b = tk.Frame(s1, bg=BRAND["card"])
        row1b.pack(fill="x", pady=(dpi.px(6), 0))
        self.diag_btn = ui_kit.button(row1b, "复制诊断信息", self._copy_diag,
                                      kind="ghost")
        self.diag_btn.pack(side="left")
        self.ver_lab = tk.Label(row1b, text="", bg=BRAND["card"],
                                fg=BRAND["faint"], font=F_SMALL, anchor="e")
        self.ver_lab.pack(side="right")
        self.ver_lab.config(text=version.display())
        # 更新行(REQ-20261026-002):检查更新 + 就绪后的「重启并更新」
        row1c = tk.Frame(s1, bg=BRAND["card"])
        row1c.pack(fill="x", pady=(dpi.px(6), 0))
        self.upd_btn = ui_kit.button(row1c, "检查更新", self._update_now,
                                     kind="ghost")
        self.upd_btn.pack(side="left")
        self.upd_restart = ui_kit.button(row1c, "重启并更新", None,
                                         kind="cta")
        self.upd_restart.pack_forget()
        self.upd_lab = tk.Label(s1, text="", bg=BRAND["card"],
                                fg=BRAND["dim"], font=F_SMALL, anchor="w",
                                wraplength=dpi.px(430), justify="left")
        self.upd_lab.pack(fill="x", pady=(dpi.px(4), 0))

        s2 = ui_kit.card(st, "开始剪辑的条件")
        pre_row = tk.Frame(s2, bg=BRAND["card"])
        pre_row.pack(fill="x")
        self.pre_labs = {}
        for key, txt in (("login", "登录"), ("member", "权益"),
                         ("bind", "微信"), ("env", "环境")):
            lab = tk.Label(pre_row, text=txt, bg=BRAND["card"],
                           fg=BRAND["dim"], font=F_BODY)
            lab.pack(side="left", padx=(0, dpi.px(12)))
            self.pre_labs[key] = lab
            if key == "env":
                lab.configure(cursor="hand2")
                lab.bind("<Button-1>", lambda e: self._env_click())
        btn_row = tk.Frame(pre_row, bg=BRAND["card"])
        btn_row.pack(side="right")
        self.bind_btn = ui_kit.button(btn_row, "去绑定", self._bind_click,
                                      kind="brand", width=7)
        self.bind_btn.pack(side="right")
        self.env_btn = ui_kit.button(btn_row, "检测", self._env_click,
                                     kind="brand", width=7)
        self.env_btn.pack(side="right", padx=(0, dpi.px(6)))
        self.env_detail = tk.Label(s2, text="", bg=BRAND["card"],
                                   fg=BRAND["dim"], font=F_SMALL, anchor="w",
                                   wraplength=dpi.px(430), justify="left")
        self.env_detail.pack(fill="x", pady=(dpi.px(6), 0))

        s3 = ui_kit.card(st, "微信与通知")
        row3 = tk.Frame(s3, bg=BRAND["card"])
        row3.pack(fill="x")
        self.set_bind_lab = tk.Label(row3, text="微信：未知", bg=BRAND["card"],
                                     fg=BRAND["fg"], font=F_BODY, anchor="w")
        self.set_bind_lab.pack(side="left", fill="x", expand=True)
        self.opt_in_var = tk.BooleanVar(value=False)
        self._opt_busy = {"on": False}
        self.opt_in_chk = ui_kit.checkbutton(
            row3, "成片通知", self.opt_in_var, command=self._on_opt_in)
        self.opt_in_chk.pack(side="right")
        self.notify_lab = tk.Label(s3, text="通知送达：未知（暂不提供查询）",
                                   bg=BRAND["card"], fg=BRAND["dim"],
                                   font=F_SMALL, anchor="w")
        self.notify_lab.pack(fill="x", pady=(dpi.px(6), 0))
        ui_kit.hint(st, "更换收片微信请联系大师姐；通知开关与绑定相互独立（关通知≠解绑）")

        s4 = ui_kit.card(st, "电脑与录像")
        from toolbox import device_identity as _di
        tk.Label(s4, text=f"本机设备 {_di.device_code()[:15]}…（一机一号，"
                          "换电脑需短信重新验证）",
                 bg=BRAND["card"], fg=BRAND["fg"], font=F_SMALL, anchor="w",
                 wraplength=dpi.px(430), justify="left").pack(fill="x")
        row4 = tk.Frame(s4, bg=BRAND["card"])
        row4.pack(fill="x", pady=(dpi.px(6), 0))
        self.set_dir_lab = tk.Label(row4, text="录像目录：未选择",
                                    bg=BRAND["card"], fg=BRAND["fg"],
                                    font=F_SMALL, anchor="w",
                                    wraplength=dpi.px(330), justify="left")
        self.set_dir_lab.pack(side="left", fill="x", expand=True)
        ui_kit.button(row4, "更换文件夹", self._browse,
                      kind="brand").pack(side="right")
        ui_kit.hint(st, "便携版：解压后运行，升级请保留 data 目录（账号、"
                        "设备与队列在里面）")

    def _nav(self, page):
        self._page_key = page
        for key, (icon_c, lab) in self._nav_icons.items():
            sel = key == page
            bg = BRAND["lav"] if sel else BRAND["side"]
            fg = BRAND["primary"] if sel else BRAND["deep"]
            icon_c.master.config(bg=bg)
            icon_c.config(bg=bg)
            ui_kit.draw_icon(icon_c,
                             {"home": "home", "works": "works",
                              "settings": "settings"}[key], fg)
            lab.config(bg=bg, fg=fg)
            self._pages[key].pack_forget()
        self._pages[page].pack(fill="both", expand=True)

    def _place_bottom_right(self):
        self.update_idletasks()
        w, h = self.winfo_width(), self.winfo_height()
        x = self.winfo_screenwidth() - w - dpi.px(36)
        y = self.winfo_screenheight() - h - dpi.px(72)
        self.geometry(f"+{max(x, 0)}+{max(y, 0)}")

    # ---------- 历史/作品页缓存 ----------

    def _load_hist(self):
        try:
            d = json.loads(HIST.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            d = {}
        return {"watch_dir": d.get("watch_dir", ""),
                "auto_record": bool(d.get("auto_record"))}

    def _save_hist(self):
        HIST.parent.mkdir(parents=True, exist_ok=True)
        HIST.write_text(json.dumps(
            {"watch_dir": self.watch_dir,
             "auto_record": self.rec_auto_var.get()
             if hasattr(self, "rec_auto_var") else self._rec_auto_on},
            ensure_ascii=False, indent=1), encoding="utf-8")

    def _load_last_works(self):
        try:
            d = json.loads(LAST_WORKS.read_text(encoding="utf-8"))
            return d if isinstance(d, dict) else {}
        except (OSError, ValueError):
            return {}

    def _save_last_works(self, session, works_url):
        if not works_url:
            return
        d = {"session": session, "works_url": works_url, "ts": time.time()}
        self._last_works = d
        try:
            LAST_WORKS.parent.mkdir(parents=True, exist_ok=True)
            LAST_WORKS.write_text(json.dumps(d, ensure_ascii=False, indent=1),
                                  encoding="utf-8")
        except OSError as e:
            LOG.info("作品页地址落盘失败:%s", e)

    # ---------- 账号 ----------

    def _acct_cfg(self):
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
            self._flash("已退出账号", BRAND["dim"])
            return
        from toolbox.account_ui import login_dialog
        got = login_dialog(self, api, prod)
        self._acct_refresh()
        if got:
            self._flash("登录成功", BRAND["primary"])

    def _mask_phone(self, phone):
        from toolbox.diagnostics import mask_phone
        return mask_phone(phone or "")

    def _render_acct_labels(self):
        """账号卡与侧栏账号行(统一入口:刷新会话、死会话清空都走这里)。"""
        sess, err = self._sess or {}, (self._pre.get("auth_error") or "")
        if sess:
            uid = str(sess.get("user_id", "?"))
            short = uid if len(uid) <= 14 else uid[:8] + "…" + uid[-4:]
            self.set_acct_lab.config(
                text=f"{short} · {self._mask_phone(sess.get('phone')) or '手机未知'}",
                fg=BRAND["fg"])
            self.acct_btn.config(text="退出")
            self.side_acct.config(text=short, fg=BRAND["fg"])
        elif err:
            self.set_acct_lab.config(text="登录已失效，请重新登录",
                                     fg=BRAND["danger"])
            self.acct_btn.config(text="重新登录")
            self.side_acct.config(text="登录已失效", fg=BRAND["danger"])
        else:
            self.set_acct_lab.config(text="未登录", fg=BRAND["dim"])
            self.acct_btn.config(text="登录")
            self.side_acct.config(text="未登录", fg=BRAND["dim"])

    def _acct_refresh(self):
        from toolbox.account import AccountClient
        api, prod = self._acct_cfg()
        try:
            sess = AccountClient(api, prod).session_summary()
        except Exception:                      # noqa: BLE001
            sess = {}
        self._sess = sess or {}
        self._render_acct_labels()
        self._env_refresh()
        if sess:
            self._env_auto()
        self._render_dir()
        self._prereq_refresh()

    # ---------- 三前置 + 环境 ----------

    def _prereq_refresh(self):
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
            try:
                self.after(0, lambda p=pre: self._render_pre(p or {}))
            except (tk.TclError, RuntimeError):
                pass
        threading.Thread(target=run, daemon=True).start()

    def _render_pre(self, pre=None):
        if pre is not None:
            self._pre = pre
        pre = self._pre
        mark = {True: ("✓", "good"), False: ("✗", "bad"),
                None: ("·", "dim")}
        auth_err = pre.get("auth_error") or ""
        if auth_err:
            # 服务端已吊销:本地会话已被 prerequisites 清掉,界面同步清账号卡
            # (无会话时也要重绘——否则侧栏停在「未登录」,与徽标「已失效」不一致)
            if self._sess:
                self._sess = {}
                self._env = {}
                self._render_env()
            self._render_acct_labels()
        if auth_err and not self._auth_notified:
            self._auth_notified = True
            msg = pre.get("auth_message") or "登录已失效，请重新登录"
            # AccountError.__str__ 形如 "[code] message"：去掉重复的码前缀
            msg = re.sub(r"^\[[^\]]+\]\s*", "", str(msg)).strip()
            self._flash(f"登录已失效：{msg}（{auth_err}）", BRAND["danger"],
                        sticky=True)
            self._tray_tip("登录已失效")
        elif not auth_err:
            self._auth_notified = False
        binding = pre.get("binding")
        for key, val in (("login", pre.get("logged_in")),
                         ("member", pre.get("member")),
                         ("bind", {"bound": True, "none": False}.get(binding))):
            glyph, color = mark[val]
            name = {"login": "登录", "member": "权益", "bind": "微信"}[key]
            self.pre_labs[key].config(text=f"{name}{glyph}", fg=BRAND[color])
        self._render_env()
        if binding == "none":
            self.bind_btn.pack(side="right")
        else:
            self.bind_btn.pack_forget()
        if binding == "bound":
            self.set_bind_lab.config(text="微信：已绑定", fg=BRAND["good"])
        elif binding == "none":
            self.set_bind_lab.config(text="微信：未绑定", fg=BRAND["bad"])
        else:
            self.set_bind_lab.config(text="微信：未知（服务未返回）",
                                     fg=BRAND["dim"])
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
                    BRAND["primary"]))
            except AccountError as e:
                def back():
                    self._opt_busy["on"] = True
                    try:
                        self.opt_in_var.set(not val)
                    finally:
                        self._opt_busy["on"] = False
                    self._flash(f"通知设置失败：{e}", BRAND["danger"])
                self.after(0, back)
        threading.Thread(target=run, daemon=True).start()

    def _bind_click(self):
        from toolbox.binding_ui import binding_dialog
        api, prod = self._acct_cfg()
        try:
            binding_dialog(self, api, prod)
        except Exception as e:                  # noqa: BLE001
            LOG.warning("绑定对话框异常：%s", e)
            self._flash("绑定流程异常，请稍后再试", BRAND["warn"])
        self._acct_refresh()

    # ---------- 环境检测(REQ-20261022-005) ----------

    def _env_result(self):
        uid = str((self._sess or {}).get("user_id") or "")
        r = self._env or {}
        if str(r.get("user_id") or uid) != uid:
            return {}
        return r

    def _render_env(self):
        from toolbox import envcheck
        logged = bool(self._sess)
        res = self._env_result() if logged else {}
        state = envcheck.result_state(res) if logged else "off"
        glyph, color = {"off": ("·", "dim"), "pass": ("✓", "good"),
                        "warn": ("!", "warn"), "fail": ("✗", "bad")}[state]
        self.pre_labs["env"].config(text=f"环境{glyph}", fg=BRAND[color])
        if not logged:
            self.env_detail.config(text="登录后自动检测这台电脑能不能稳定剪片"
                                        "（约 1-2 分钟）", fg=BRAND["dim"])
        elif not res:
            self.env_detail.config(text="本账号还没检测这台电脑，登录后会自动开始"
                                        "（约 1-2 分钟）", fg=BRAND["dim"])
        else:
            self.env_detail.config(text=envcheck.summary_line(res),
                                   fg=BRAND["dim"])
        self.env_btn.config(state="normal" if logged and not self._env_busy
                            else "disabled")

    def _env_refresh(self):
        from toolbox import envcheck
        self._env = envcheck.last_result()
        self._render_env()

    def _env_auto(self):
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
            self._flash("登录后会自动检测环境", BRAND["warn"])
            return
        if self._env_busy:
            return
        if self._env_result():
            self._env_dialog()
        else:
            self._env_run(manual=True)

    def _env_run(self, manual=True):
        if self._env_busy:
            return
        self._env_busy = True
        self.env_btn.config(state="disabled")
        from toolbox import envcheck
        wdir = self.watch_dir or (load_config().get("folder_watch")
                                  or {}).get("watch_dir")
        uid = str((self._sess or {}).get("user_id") or "")
        t0 = time.time()
        self._flash("开始检测这台电脑…", BRAND["primary"])

        def _ui(fn):
            try:
                self.after(0, fn)
            except (tk.TclError, RuntimeError):
                pass

        def progress(frac, text):
            _ui(lambda t=text, f=frac: self.env_detail.config(
                text=f"{t}（{int(max(0.0, min(1.0, f)) * 100)}%）",
                fg=BRAND["primary"]))

        def run():
            try:
                res = envcheck.run(progress=progress, watch_dir=wdir,
                                   user_id=uid)
            except Exception as e:              # noqa: BLE001
                LOG.warning("环境检测异常:%s", e)
                res = None
            _ui(lambda r=res: self._env_done(r, t0, manual))
        threading.Thread(target=run, daemon=True, name="envcheck").start()

    def _env_done(self, result, t0, manual):
        self._env_busy = False
        if not result:
            self._flash("环境检测出错了，稍后可再点「重新检测」", BRAND["danger"],
                        sticky=True)
            self._render_env()
            return
        self._env = result
        self._render_env()
        cost = int(time.time() - t0)
        if result.get("incomplete"):
            self._flash("环境检测未完成（体验素材下载失败），"
                        "检查网络后点「重新检测」", BRAND["warn"], sticky=True)
            self._tray_tip("环境检测未完成")
        elif result.get("ok"):
            head = ("环境检测通过" if not result.get("warn")
                    else f"环境可用（{result.get('warn')} 项提示）")
            self._flash(f"{head}，用时 {cost} 秒", BRAND["good"], sticky=True)
            self._tray_tip("环境检测通过")
            if manual:
                self._env_dialog()
        else:
            bad = "、".join(i["label"] for i in result.get("items", [])
                            if i.get("level") == "bad")
            self._flash(f"环境检测发现问题：{bad}", BRAND["danger"],
                        sticky=True)
            self._tray_tip("环境检测异常")
            self._env_dialog()

    def _env_dialog(self):
        from toolbox import envcheck
        r = self._env_result()
        win = tk.Toplevel(self)
        win.title("环境检测")
        win.resizable(False, False)
        win.configure(bg=BRAND["page"])
        win.transient(self)
        f = ttk.Frame(win, padding=(dpi.px(16), dpi.px(12)))
        f.pack(fill="both", expand=True)
        head = ("检测未完成" if r.get("incomplete")
                else "环境可用" if r.get("ok") else "这台电脑暂时不能剪片")
        color = (BRAND["warn"] if r.get("incomplete")
                 else BRAND["good"] if r.get("ok") else BRAND["danger"])
        tk.Label(f, text=head, bg=BRAND["page"], fg=color,
                 font=F_DIALOG).pack(anchor="w")
        if r.get("checked_at"):
            tk.Label(f, text=f"检测时间 {r['checked_at']}",
                     bg=BRAND["page"], fg=BRAND["dim"],
                     font=F_SMALL).pack(anchor="w")
        for icon_ch, text in envcheck.detail_lines(r):
            row = tk.Frame(f, bg=BRAND["page"])
            row.pack(fill="x", pady=dpi.px(1))
            tk.Label(row, text=icon_ch, bg=BRAND["page"],
                     fg={"✓": BRAND["good"], "!": BRAND["warn"],
                         "✗": BRAND["danger"]}.get(icon_ch, BRAND["dim"]),
                     font=(FONT, 9, "bold"), width=2).pack(side="left")
            tk.Label(row, text=text, bg=BRAND["page"], fg=BRAND["fg"],
                     font=F_BODY, anchor="w", wraplength=dpi.px(400),
                     justify="left").pack(side="left", fill="x", expand=True)
        est = (r.get("estimate") or {}).get("min_per_30")
        if est:
            tk.Label(f, text="说明：识别耗时按每秒 1 帧、多进程并行估算；"
                             "真实链路只识别画面局部，通常更快。",
                     bg=BRAND["page"], fg=BRAND["dim"], font=F_SMALL,
                     wraplength=dpi.px(420), justify="left").pack(
                anchor="w", pady=(dpi.px(6), 0))
        if not r.get("ok"):
            tk.Label(f, text="可以先按上面的提示处理；处理不了就把这张单子"
                             "截图发给大师姐。",
                     bg=BRAND["page"], fg=BRAND["warn"], font=F_SMALL,
                     wraplength=dpi.px(420), justify="left").pack(
                anchor="w", pady=(dpi.px(4), 0))
        row = tk.Frame(f)
        row.pack(fill="x", pady=(dpi.px(10), 0))
        ui_kit.button(row, "重新检测", lambda: (win.destroy(),
                                              self._env_run(True)),
                      kind="brand").pack(side="left", padx=(0, dpi.px(8)))
        ui_kit.button(row, "关闭", win.destroy,
                      kind="brand").pack(side="left")
        win.wait_visibility()
        win.grab_set()

    # ---------- 生产状态 ----------

    def _prod_refresh(self):
        import toolbox.folder_watch as fw

        def run():
            out = {}
            try:
                wdir = (load_config().get("folder_watch") or {}).get(
                    "watch_dir") or self.watch_dir
                out["wdir"] = wdir
                if wdir:
                    out["summary"] = fw.summary()
                    out["jobs"] = fw.jobs_view()
                    out["last_ts"] = fw.last_production_ts()
                    out["due"] = fw.production_due(boot=False)
                    out["added"] = fw.unprocessed_bytes(wdir)
                    out["limit"] = fw.threshold_bytes()
                    out["next_scan_ts"] = fw.next_scan_ts()
                    out["progress"] = fw.running_progress()
                    out["scan"] = fw.scan_stats(wdir)
            except Exception as e:              # noqa: BLE001
                LOG.info("生产状态汇总失败：%s", e)
            out["produce"] = dict(self._produce)
            out["last_works"] = self._load_last_works()
            try:
                self.after(0, lambda o=out: self._render_prod(o))
            except (tk.TclError, RuntimeError):
                pass
        threading.Thread(target=run, daemon=True).start()

    def _cloud_state(self, prod):
        """-> ("running"|"done"|"failed"|"", 详情)。仅依据 produce 快照。"""
        prods = prod.get("produce") or {}
        for session, v in prods.items():
            st = (v or {}).get("state")
            if st in ("waiting", "processing"):
                return "running", session
            if st == "failed":
                return "failed", (v or {}).get("error") or ""
        lw = prod.get("last_works") or {}
        try:
            ts = float(lw.get("ts") or 0)
        except (TypeError, ValueError):
            ts = 0
        if lw.get("works_url") and time.time() - ts < 86400:
            return "done", time.strftime("%H:%M", time.localtime(ts))
        return "", ""

    def _render_prod(self, prod=None):
        if prod is not None:
            self._prod = prod
        prod, pre = self._prod, self._pre
        q = prod.get("summary") or {}
        self.queue_lab.config(
            text=(f"排队 {q.get('queued', 0)} · 处理中 "
                  f"{q.get('running', 0)} · 已完成 {q.get('done', 0)}"
                  + (f" · 失败 {len(q.get('failed', []))}"
                     if q.get("failed") else "")) if q
            else "本机处理队列")
        self._render_queue_rows(prod)

        api, _ = self._acct_cfg()
        cloud, cloud_detail = self._cloud_state(prod)
        badge, detail, act, cmd = "…", "", None, None
        if not prod.get("wdir"):
            badge, detail = "选择录像文件夹", "点「更换文件夹」选好目录，之后就全自动了"
        elif pre.get("auth_error"):
            badge = "登录已失效"
            detail = ("这台电脑的登录已被服务端撤销（换机或安全策略）。"
                      "点右侧重新登录，登录后会继续自动剪辑")
            act, cmd = "重新登录", self._acct_click
        elif api and not pre.get("logged_in"):
            badge, detail = "未登录", "登录后就会自动为你剪辑（每天到点自动开始）"
            act, cmd = "去登录", self._acct_click
        elif pre.get("binding") == "none":
            badge, detail = "微信还没绑定", "绑定微信后今天就帮你补上"
            act, cmd = "去绑定", self._bind_click
        elif pre.get("member") is False:
            badge, detail = "还没有开通权益", "联系大师姐开通，开通后自动生效"
        elif q.get("running") or cloud == "running":
            pr = prod.get("progress") or {}
            if q.get("running") and pr.get("stage"):
                badge = "剪辑中…"
                extra = ""
                if pr.get("frac"):
                    extra = f" · {int(pr['frac'] * 100)}%"
                    if pr.get("eta_min") is not None:
                        extra += " · 预计还需 " + _eta_text(pr["eta_min"])
                detail = (f"正在处理本机录像：{pr.get('stage')}"
                          f"（第 {pr.get('index', 1)}/{pr.get('total', 9)} 步"
                          f"{extra}）")
            elif cloud == "running":
                badge = "云端生成中…"
                detail = "录像已上传，云端正在生成 3 条候选（约 20 分钟）"
            else:
                badge, detail = "剪辑中…", "正在处理你的录像，完成后自动出候选"
            act, cmd = "看队列", lambda: self._pages["home"].scroll_top()
        elif cloud == "done":
            badge = f"今天已出片 {cloud_detail}"
            detail = "候选已生成，去「我的作品」打开作品页挑一条"
            act, cmd = "看作品", lambda: self._nav("works")
        elif cloud == "failed":
            badge = "云端生成失败"
            detail = ("云端没能生成成片（本机录像与队列不受影响）。"
                      f"{str(cloud_detail)[-80:]}")
            act, cmd = "复制诊断信息", self._copy_diag
        elif prod.get("last_ts"):
            t = time.strftime("%H:%M", time.localtime(prod["last_ts"]))
            badge = "结果待确认"
            detail = (f"本机 {t} 已处理并提交，云端成片尚未确认；"
                      "有结果后这里会显示「今天已出片」")
        elif prod.get("added") is not None:
            gb = prod["added"] / 1e9
            hit = prod["added"] > prod.get("limit", 2e9)
            badge = "已达标，等自动剪辑" if hit else "录像还不够"
            detail = (f"待处理录像 {gb:.2f}GB（没处理过的都算）"
                      + ("，够了！到点自动开始剪辑" if hit
                         else "，再攒一点（要超过 2GB 才开始剪）"))
            due = prod.get("due") or (False, "")
            if not hit and due[1]:
                detail += f"（{due[1]}）"
        env = self._env_result()
        if env and not env.get("ok") and badge not in ("剪辑中…",):
            badge = "环境检测未通过"
            detail = "点「账号设置 → 环境」看哪一项不行，处理后重新检测"
            act, cmd = "看检测", self._env_click
        self.prod_badge.config(text=badge)
        color = {"未登录": BRAND["bad"], "登录已失效": BRAND["bad"],
                 "微信还没绑定": BRAND["warn"],
                 "还没有开通权益": BRAND["bad"], "剪辑中…": BRAND["primary"],
                 "云端生成中…": BRAND["primary"], "选择录像文件夹": BRAND["dim"],
                 "录像还不够": BRAND["dim"], "结果待确认": BRAND["warn"],
                 "云端生成失败": BRAND["danger"],
                 "环境检测未通过": BRAND["warn"]}.get(badge, BRAND["good"])
        self.prod_badge.config(fg=color)
        self.prod_detail.config(text=detail)
        if act:
            self.prod_act.config(text=act, command=cmd)
            self.prod_act.pack(side="right")
        else:
            self.prod_act.pack_forget()
        self._render_works(prod, cloud, cloud_detail)
        self._render_dir()

    def _render_works(self, prod, cloud, cloud_detail):
        """我的作品:真实成片才显示;无作品给空态与下一步(REQ-007 V14)。"""
        lw = prod.get("last_works") or {}
        if cloud == "done" and lw.get("works_url"):
            self.works_empty.pack_forget()
            self.works_lab.config(
                text=f"最近一次成片：今天 {cloud_detail}\n"
                     "3 条候选已生成，点右侧打开作品页挑一条最好的",
                fg=BRAND["fg"])
            self.works_btn.pack(side="right")
            self.works_btn.config(state="normal")
            return
        if cloud == "running":
            self.works_empty.pack_forget()
            self.works_lab.config(text="云端正在生成 3 条候选（约 20 分钟）\n"
                                       "完成后点右侧打开作品页挑选",
                                  fg=BRAND["fg"])
            self.works_btn.pack(side="right")
            self.works_btn.config(state="disabled")
            return
        for w in self.works_empty.winfo_children():
            w.destroy()
        box = ui_kit.empty_state(
            self.works_empty, "还没有真实成片",
            ("先在工作台选好录像文件夹，之后每天会自动为你剪出候选"
             if not prod.get("wdir") else
             "录像攒够 2GB（没处理过的都算）会自动开始剪辑；"
             "剪好之前这里不会有内容"))
        box.pack(fill="x", pady=dpi.px(4))
        self.works_empty.pack(fill="x")
        self.works_lab.config(text="", fg=BRAND["dim"])
        self.works_btn.pack(side="right")
        self.works_btn.config(state="disabled")

    def _wait_reason(self, j, prod):
        import toolbox.folder_watch as fw
        st = j.get("state")
        if st == "running":
            pr = prod.get("progress") or {}
            if pr.get("session") == j.get("session") and pr.get("stage"):
                head = f"{pr['stage']}（第 {pr.get('index', 1)}/{pr.get('total', 9)} 步）"
                tail = []
                if pr.get("frac"):
                    tail.append(f"{int(pr['frac'] * 100)}%")
                if pr.get("eta_min") is not None:
                    tail.append(_eta_text(pr["eta_min"]))
                return head + (" · " + " · ".join(tail) if tail else "")
            return "正在识别本机录像…"
        if st == "failed":
            n = (j.get("note") or "").strip()
            return ("上次失败：" + n[-90:]) if n else "上次失败：原因未记录"
        if st == "queued":
            if not fw.target_stable(j.get("path")):
                return "等待录像写完（文件还在增长）"
            nxt = prod.get("next_scan_ts") or 0
            if nxt > time.time():
                return ("已就绪，下次自动检查 "
                        + time.strftime("%H:%M", time.localtime(nxt)))
            due = prod.get("due")
            if due and not due[0] and due[1]:
                return "已就绪，等生产时点：" + due[1]
            return "已就绪，排在前面任务之后"
        return (j.get("note") or "").strip()

    def _render_queue_rows(self, prod):
        jobs = prod.get("jobs") or []
        box = self.queue_rows
        for w in box.winfo_children():
            w.destroy()
        if not jobs:
            tk.Label(box, text="还没有任务。选好录像文件夹后，新录像会自动排队。",
                     bg=BRAND["card"], fg=BRAND["dim"], font=F_SMALL,
                     anchor="w", justify="left",
                     wraplength=dpi.px(420)).pack(fill="x", pady=dpi.px(4))
            return
        for i, j in enumerate(jobs, 1):
            row = tk.Frame(box, bg=BRAND["card"])
            row.pack(fill="x", pady=(0, dpi.px(2)))
            st = j.get("state")
            stc = {"running": BRAND["primary"], "done": BRAND["good"],
                   "failed": BRAND["danger"]}.get(st, BRAND["warn"])
            tk.Label(row, text=f"{i}.", bg=BRAND["card"], fg=BRAND["faint"],
                     font=F_SMALL, width=2, anchor="nw").pack(
                side="left", anchor="n", pady=(dpi.px(1), 0))
            tk.Label(row, text=self.STATE_TXT.get(st, st), bg=BRAND["card"],
                     fg=stc, font=F_SMALL, width=5, anchor="nw").pack(
                side="left", anchor="n", pady=(dpi.px(1), 0))
            col = tk.Frame(row, bg=BRAND["card"])
            col.pack(side="left", fill="x", expand=True, anchor="n")
            name = j.get("session") or ""
            tk.Label(col, text=name[:26] + ("…" if len(name) > 26 else ""),
                     bg=BRAND["card"], fg=BRAND["fg"], font=F_BODY,
                     anchor="w").pack(fill="x")
            reason = self._wait_reason(j, prod)
            tk.Label(col, text=reason, bg=BRAND["card"], fg=BRAND["dim"],
                     font=F_SMALL, anchor="w", justify="left",
                     wraplength=dpi.px(300)).pack(fill="x")
            ui_kit.button(row, "移除", lambda k=j.get("key"): self._queue_remove(k),
                          kind="ghost").pack(side="right")
        if len(jobs) > 1:
            tk.Label(box, text=f"共 {len(jobs)} 个任务", bg=BRAND["card"],
                     fg=BRAND["faint"], font=F_SMALL, anchor="w").pack(
                fill="x", pady=(dpi.px(2), 0))

    def _queue_remove(self, key):
        import toolbox.folder_watch as fw
        if not messagebox.askyesno(
                "移除任务",
                "只从队列里移除这次待处理记录，不会删除你的录像文件。\n"
                "如果录像还在文件夹里，下次扫描可能会重新排队。\n\n确定移除？"):
            return
        j = fw.remove(key)
        if j:
            self._flash("已从队列移除（录像文件未删除）", BRAND["dim"])
        self._prod_refresh()

    def _open_portal(self):
        from toolbox.uploader import works_full_url
        cfg = load_config()
        url = ((cfg.get("account") or {}).get("portal_url") or "").strip()
        if not url:
            last = self._load_last_works()
            origin = ((cfg.get("server") or {}).get("upload_url") or "").strip()
            url = works_full_url(origin, last.get("works_url") or "")
        if not url:
            self._flash("还没有真实成片可看：剪辑完成后会自动生成作品页",
                        BRAND["warn"])
            return
        webbrowser.open(url)

    def _render_dir(self):
        wdir = (self.watch_dir
                or (load_config().get("folder_watch") or {}).get("watch_dir")
                or "")
        wdir = str(wdir)
        short = wdir if len(wdir) <= 44 else wdir[:42] + "…"
        self.dir_lab.config(text=short or "未选择",
                            fg=BRAND["fg"] if wdir else BRAND["dim"])
        self.set_dir_lab.config(text=f"录像目录：{short or '未选择'}")
        scan = (self._prod or {}).get("scan") or {}
        if wdir and scan:
            self.dir_note.config(
                text=f"最近检查 {scan.get('last_scan', '未知')} · 发现 "
                     f"{scan.get('videos', 0)} 个视频 · 待处理 "
                     f"{scan.get('pending', 0)} 个"
                     f"（每 {scan.get('interval_min', 120)} 分钟自动检查一次，"
                     "文件写完才处理）")
        elif wdir:
            self.dir_note.config(text="会自动检查新增录像，文件写完才处理")
        else:
            self.dir_note.config(text="选一个存放游戏录像的文件夹即可")

    # ---------- 诊断(REQ-20261022-010) ----------

    def _copy_diag(self):
        from toolbox import diagnostics
        from toolbox import rules_sync
        rules_meta = rules_sync.applied_meta()
        upd = self._upd_info or {}
        upd_line = ("更新：当前 " + version.display()
                    + ("；新版本 v" + str((upd.get("latest") or {}).get(
                        "version", "?")) + " 已就绪待重启"
                       if self._upd_phase == "ready" else
                       "；无可用更新" if self._upd_phase in ("none", "disabled")
                       else "；状态 " + str(self._upd_phase)))
        rules_line = ("规则版本：内置默认（未拉取到服务端规则）"
                      if not rules_meta else
                      "规则版本：v" + str(rules_meta.get("rules_version", "?"))
                      + "（" + str(rules_meta.get("applied_at", "")) + " 对齐）")
        try:
            text = diagnostics.build(
                version.label(), sess=self._sess, pre=self._pre,
                env=self._env_result(), prod=self._prod,
                jobs=(self._prod.get("jobs") or []),
                summary=(self._prod.get("summary") or {}),
                produce=self._produce,
                extra_lines=[
                    (f"登录状态：{self._pre.get('auth_error')} "
                     f"{self._pre.get('auth_message', '')}"
                     if self._pre.get("auth_error") else "登录状态：正常"),
                    f"本地云端记录 {len(self._produce)} 条",
                    rules_line,
                    upd_line])
            path = diagnostics.save(text)
            self.clipboard_clear()
            self.clipboard_append(text)
            msg = "诊断信息已复制（不含手机号/令牌）"
            if path:
                msg += "，也存到 data/diagnostics/last-report.txt"
            self._flash(msg, BRAND["good"], sticky=True)
        except Exception as e:                  # noqa: BLE001
            LOG.warning("诊断生成失败:%s", e)
            self._flash("诊断信息生成失败，请稍后再试", BRAND["danger"])

    def _all_refresh(self):
        self._prereq_refresh()
        self._prod_refresh()

    # ---------- 目录选择 ----------

    def _browse(self):
        d = filedialog.askdirectory(initialdir=str(ROOT / "video"),
                                    title="选择录像目录（后台自动扫描）")
        if d:
            self._set_watch_dir(str(Path(d)))

    def _set_watch_dir(self, d):
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
        self._flash("录像目录已设置，正在检查这个文件夹…", BRAND["primary"])
        self._prod_refresh()
        # 换目录立即扫一次(2026-10-27 用户反馈:换目录后任务最长 2h 不更新,
        # 等下个扫描拍太迟钝)。后台线程扫,完成后 UI 自动出现新任务;
        # 之后仍按正常 2h 节拍巡扫。
        def scan_now():
            import toolbox.folder_watch as fw
            try:
                new = fw.scan(d)
                if new:
                    LOG.info("换目录即时扫描：%d 个新目标", new)
            except Exception as e:              # noqa: BLE001 失败不影响主流程
                LOG.warning("换目录即时扫描失败：%s", e)
            self._ui(self._prod_refresh)
        threading.Thread(target=scan_now, daemon=True,
                         name="scan-on-dir-change").start()

    # ---------- 桌面录制 ----------

    REC_WATCH_S = 10          # 游戏进程轮询周期
    REC_MISS_N = 3            # 连续 N 拍不见游戏才停(防启动器切换抖动)

    def _rec_auto_toggle(self):
        """「自动录制」开关:开=起监控线程(游戏在则立即录);关=停录+停监控。"""
        on = self.rec_auto_var.get()
        self._rec_auto_on = on
        hist = self._load_hist()
        hist["auto_record"] = on
        try:
            import json as _json
            HIST.parent.mkdir(parents=True, exist_ok=True)
            HIST.write_text(_json.dumps(hist, ensure_ascii=False, indent=1),
                            encoding="utf-8")
        except OSError:
            pass
        if on:
            self._flash("自动录制已开启：进三角洲行动就自动录", BRAND["primary"])
            self._rec_render()          # 立即显示「等待三角洲行动启动」
            self._rec_watch_start()
        else:
            self._auto_wake.set()          # 唤醒线程退出
            if self._rec is not None and not self._rec_stopping:
                self._rec_stop()
            self._rec_render()
            self._flash("自动录制已关闭", BRAND["dim"])

    def _rec_watch_start(self):
        """游戏监控线程:游戏启动→录;退出(连续 N 拍)→停录。幂等。"""
        if getattr(self, "_rec_watch_on", False):
            self._auto_wake.set()           # 已有线程:先唤醒(新拍即刻生效)
            return
        self._rec_watch_on = True
        _scr_cfg = load_config().get("screenrec") or {}
        names = _scr_cfg.get("game_processes") or ["delta_force", "deltaforce"]
        exclude = _scr_cfg.get("game_process_exclude")

        def loop():
            from toolbox.screenrec import game_running
            miss = 0
            while self._rec_auto_on and not self.daily_stop.is_set():
                try:
                    running = game_running(names, exclude)
                except Exception:           # noqa: BLE001 探测失败按未运行
                    running = False
                if running:
                    miss = 0
                    if self._rec is None and not self._rec_stopping:
                        self.after(0, self._rec_start)
                else:
                    miss += 1
                    if miss >= self.REC_MISS_N and self._rec is not None \
                            and not self._rec_stopping:
                        miss = 0
                        self.after(0, self._rec_stop)
                self._auto_wake.wait(self.REC_WATCH_S)
                self._auto_wake.clear()
            self._rec_watch_on = False
        threading.Thread(target=loop, daemon=True,
                         name="rec-game-watch").start()

    def _rec_start(self):
        wdir = (self.watch_dir
                or (load_config().get("folder_watch") or {}).get("watch_dir"))
        if not wdir:
            self._flash("先选好录像文件夹，录制会保存到那里", BRAND["warn"])
            return
        from toolbox.screenrec import ScreenRecorder
        cfg = dict(load_config().get("screenrec") or {})
        self._rec_stopping = False
        self._rec = ScreenRecorder(Path(wdir), cfg,
                                   on_event=self._rec_event_bg)
        self._rec.start()
        self.rec_lab.config(text="启动中…", fg=BRAND["fg"])
        self._flash("录制已开始，保存到录像文件夹（每天一个子文件夹）",
                    BRAND["primary"])
        self.after(1000, self._rec_tick)

    def _rec_stop(self):
        rec = self._rec
        if rec is None:
            return
        self._rec_stopping = True
        self.rec_auto_chk.configure(state="disabled")
        self.rec_lab.config(text="收尾中…（正在写完当前文件）",
                            fg=BRAND["dim"])
        self.rec_pause_btn.pack_forget()

        def fin():
            try:
                files = rec.stop(timeout=40)
            except Exception as e:        # noqa: BLE001
                LOG.warning("停录异常：%s", e)
                files = []
            # 兜底:正常路径由录制器发 stop 事件复位 UI;若录制器异常退出
            # 没发事件,这里强制复位——否则界面永远卡在「收尾中…」
            def fallback():
                if self._rec is rec:
                    self._rec_event("stop", {"secs": rec.elapsed(),
                                             "files": files or []})
            try:
                self.after(1500, fallback)
            except Exception:             # noqa: BLE001 窗口已销毁
                pass
        threading.Thread(target=fin, daemon=True).start()

    def _rec_pause(self):
        rec = self._rec
        if rec is None or self._rec_stopping:
            return
        if rec.paused:
            rec.resume()
        else:
            rec.pause()
        self._rec_render()

    def _rec_event_bg(self, kind, payload):
        """录制器回调在工作线程触发，统一 marshal 回 UI 线程。"""
        try:
            self.after(0, self._rec_event, kind, payload)
        except Exception:                 # noqa: BLE001 退出路径窗口已销毁
            pass

    def _rec_event(self, kind, payload):
        if kind == "error":
            self._flash(payload.get("msg", ""), BRAND["warn"], sticky=True)
            return
        if kind != "stop":
            self._rec_render()
            return
        # stop：整次录制结束（files 已收尾完整）
        secs = payload.get("secs", 0)
        files = payload.get("files", [])
        self._rec = None
        self._rec_stopping = False
        self.rec_auto_chk.configure(state="normal")
        self._rec_render()
        self.rec_note.config(
            text=f"已录 {self._fmt_secs(secs)} · {len(files)} 个分P，"
                 "约 10 分钟后整个文件夹自动排队剪辑")
        self._flash(f"录制结束，共 {len(files)} 个文件", BRAND["primary"])
        threading.Thread(target=self._rec_scan_after_stop, daemon=True).start()
        self._prod_refresh()

    def _rec_scan_after_stop(self):
        """录完即扫一次队列（幂等）；文件判稳仍要 10 分钟，此处只提前
        登记时刻，真正入队由本次或后续巡扫完成。"""
        try:
            import toolbox.folder_watch as fw
            if self.watch_dir:
                fw.scan(self.watch_dir)
        except Exception as e:            # noqa: BLE001 扫描失败不惊扰
            LOG.info("录后扫描跳过：%s", e)

    def _rec_tick(self):
        """录制中每秒刷状态行（时长/P 号）；录制器清空后自停。"""
        if self._rec is None:
            return
        if not self._rec_stopping:
            self._rec_render()
        self.after(1000, self._rec_tick)

    def _rec_render(self):
        rec = self._rec
        if rec is None:
            if self._rec_auto_on:
                self.rec_lab.config(text="等待三角洲行动启动…",
                                    fg=BRAND["dim"])
            else:
                self.rec_lab.config(text="自动录制未开启", fg=BRAND["dim"])
            self.rec_pause_btn.pack_forget()
            self.rec_note.config(
                text="开着自动录制：进三角洲行动就自动录，退游戏自动收尾，"
                     "录完整个文件夹自动排队剪辑")
            return
        self.rec_lab.config(fg=BRAND["fg"])
        if rec.paused:
            self.rec_lab.config(
                text=f"已暂停 · 已录 {self._fmt_secs(rec.elapsed())}")
            self.rec_pause_btn.config(text="继续")
        else:
            self.rec_lab.config(
                text=f"录制中 P{rec._cur_part or 0}"
                     f" · {self._fmt_secs(rec.elapsed())}")
            self.rec_pause_btn.config(text="暂停")
        self.rec_pause_btn.pack(side="right", padx=(0, dpi.px(8)),
                                before=self.rec_auto_chk)

    @staticmethod
    def _fmt_secs(s):
        s = int(s)
        h, m, sec = s // 3600, s % 3600 // 60, s % 60
        return f"{h}:{m:02d}:{sec:02d}" if h else f"{m}:{sec:02d}"

    # ---------- 后台日任务 ----------

    RULES_POLL_S = 6 * 3600

    def _rules_start(self):
        """规则服务端对齐(REQ-20261026-001 规则2):首启 30s 拉一次,
        之后每 6h 一次;失败沿用缓存/内置默认,永不阻塞。规则只含非安全
        参数(阈值/节拍等),经白名单+夹紧,登录/绑定等安全门不在此列。"""
        def loop():
            import toolbox.rules_sync as rs
            if self.daily_stop.wait(30):
                return
            try:
                rs.poll_once()
            except Exception as e:              # noqa: BLE001
                LOG.info("规则对齐跳过:%s", e)
            while not self.daily_stop.wait(rs.POLL_S):
                try:
                    rs.poll_once()
                except Exception:               # noqa: BLE001
                    pass
        threading.Thread(target=loop, daemon=True,
                         name="rules-sync").start()

    # ---------- 自动更新(REQ-20261026-002) ----------
    #
    # 后台线程:首启 60s 查一次,之后每 6h;发现新版→让路剪辑下载→
    # 校验+staged→「新版本已就绪」。**绝不自动重启**:重启入口只在用户
    # 点「重启并更新」时出现;剪辑/上传进行中既不下载也不催促。
    # 清单必须过 Ed25519 验签(update_signing),失败沿用当前版本留痕。

    UPDATE_POLL_S = 6 * 3600

    def _update_start(self):
        def loop():
            if not self.daily_stop.wait(60):
                self._update_cycle(manual=False)
            while not self.daily_stop.wait(self.UPDATE_POLL_S):
                self._update_cycle(manual=False)
                # 有新版但让路中(剪辑占线/磁盘不足)→ 10 分钟粒度重试
                tries = 0
                while (self._upd_phase == "deferred"
                       and tries < 35
                       and not self.daily_stop.wait(600)):
                    self._update_cycle(manual=False)
                    tries += 1
        threading.Thread(target=loop, daemon=True,
                         name="auto-update").start()

    def _producing_now(self):
        return bool((self._prod.get("summary") or {}).get("running")) or \
            bool(self._prod.get("progress"))

    def _update_cycle(self, manual=False):
        import toolbox.auto_update as au
        if self._upd_phase in ("ready", "downloading"):
            return
        try:
            info = au.check()
        except Exception as e:                  # noqa: BLE001
            LOG.warning("更新检查异常:%s", e)
            info = {"unavailable": True}
        if info.get("unavailable") or info.get("invalid"):
            self._upd_phase = "none"
            why = "清单验签失败，已忽略" if info.get("invalid") else "暂不可达"
            self._ui(lambda m=manual, w=why: self._upd_render(
                f"检查更新：{w}（沿用当前版本）", warn=not m))
            return
        if info.get("disabled"):
            self._upd_phase = "disabled"
            self._ui(lambda: self._upd_render("更新通道已停用"))
            return
        if not info.get("newer"):
            self._upd_phase = "none"
            if manual:
                self._ui(lambda: self._upd_render(
                    "已是最新版本"))
            else:
                self._ui(lambda: self._upd_render(""))
            return
        latest = info.get("latest") or {}
        self._upd_info = info
        build = str(latest.get("build") or "")
        note = str(latest.get("notes") or "")[:80]
        head = (f"发现新版本（{build}）"
                + ("【建议尽快升级】" if info.get("required") else ""))
        if self._producing_now():
            self._upd_phase = "deferred"
            self._ui(lambda h=head: self._upd_render(
                h + "：正在剪辑，完成后自动下载", warn=True))
            return
        if not au.disk_ok(int(info.get("size") or 0)):
            self._upd_phase = "deferred"
            self._ui(lambda h=head: self._upd_render(
                h + "：磁盘空间不足，清理后自动重试", warn=True))
            return
        self._upd_phase = "downloading"
        self._ui(lambda h=head: self._upd_render(h + "：下载中 0%"))

        def on_progress(f):
            self._ui(lambda f=f: (
                None if self._upd_phase != "downloading" else
                self.upd_lab.config(
                    text=head + f"：下载中 {int(f * 100)}%")))

        try:
            urls = info.get("pkg_urls") or [_abs_url(info)]
            zp = au.download_any(urls, str(latest.get("sha256")),
                                 int(info.get("size") or 0),
                                 au.zip_path(build), on_progress=on_progress)
            au.stage(build, zp)
            au.cleanup_old(build)
            au.save_ready({"version": str(latest.get("version")),
                           "build": build,
                           "sha256": str(latest.get("sha256")),
                           "notes": str(latest.get("notes") or "")[:200],
                           "required": bool(info.get("required")),
                           "at": time.strftime("%Y-%m-%d %H:%M:%S")})
            self._upd_phase = "ready"
            self._ui(lambda h=head, n=note: self._upd_ready_ui(h, n))
        except Exception as e:                  # noqa: BLE001 失败下一拍重试
            LOG.warning("更新下载失败:%s", e)
            self._upd_phase = "none"
            self._ui(lambda h=head, err=str(e)[:60]: self._upd_render(
                h + f"：下载失败（{err}），稍后自动重试", warn=True))

    def _upd_ready_ui(self, head, note):
        self._upd_render(head + "：已就绪"
                         + ("（更新说明：" + note + "）" if note else ""))
        self.upd_restart.config(
            command=self._update_restart,
            text=("重启并升级" if (self._upd_info or {}).get("required")
                  else "重启并更新"))
        self.upd_restart.pack(side="left", padx=(dpi.px(8), 0))
        self._tray_tip("新版本已就绪")

    def _update_restore_ready(self):
        """重启后恢复「已就绪」状态(staged 还在,直接可应用)。"""
        import toolbox.auto_update as au
        r = au.ready_info()
        if not r or not (au.stage_dir(r.get("build", ""))
                         / "GameBuddy" / "GameBuddy.exe").is_file():
            return
        self._upd_phase = "ready"
        self._upd_info = {"latest": {"version": r.get("version"),
                                      "build": r.get("build"),
                                      "notes": r.get("notes", "")},
                          "required": r.get("required")}
        self.after(800, lambda: self._upd_ready_ui(
            f"新版本（{r.get('build')}）",
            str(r.get("notes") or "")[:80]))

    def _update_now(self):
        """手动检查入口。"""
        if self._upd_phase in ("downloading", "ready"):
            return
        self._upd_render("检查更新中…")
        threading.Thread(target=lambda: self._update_cycle(manual=True),
                         daemon=True).start()

    def _update_restart(self):
        """用户确认重启并应用更新;剪辑进行中不给入口。"""
        import toolbox.auto_update as au
        if self._producing_now():
            self._flash("正在剪辑，完成后再来重启更新", BRAND["warn"])
            return
        build = str(((self._upd_info or {}).get("latest") or {}).get("build")
                    or au.ready_info().get("build") or "")
        sd = au.stage_dir(build)
        if not (sd / "GameBuddy" / "GameBuddy.exe").is_file():
            self._flash("更新文件不完整，请重新检查更新", BRAND["warn"])
            self._upd_phase = "none"
            self.upd_restart.pack_forget()
            return
        if not messagebox.askyesno(
                "重启并更新",
                "更新会在重启时自动完成（约 1-2 分钟），\n"
                "账号、队列与设置都会保留。\n\n确定现在重启？"):
            return
        try:
            app_dir = (Path(sys.executable).resolve().parent
                       if getattr(sys, "frozen", False) else Path(ROOT))
            cmd = au.build_apply_cmd(
                app_dir, sd, os.getpid(), au.UPDATE_DIR / "apply.log")
            au.launch_apply(cmd)
        except Exception as e:                  # noqa: BLE001
            LOG.warning("更新应用脚本生成失败:%s", e)
            self._flash(f"无法启动更新：{str(e)[:60]}", BRAND["danger"])
            return
        self._flash("正在退出并应用更新…", BRAND["primary"])
        self.after(400, self._on_close)

    def _upd_render(self, text, warn=False):
        self.upd_lab.config(text=text,
                            fg=BRAND["warn"] if warn else BRAND["dim"])

    def _daily_start(self):
        if self.daily_thread and self.daily_thread.is_alive():
            return
        self.daily_stop.clear()
        self.daily_thread = threading.Thread(target=self._daily_loop,
                                             daemon=True)
        self.daily_thread.start()

    def _daily_loop(self):
        import toolbox.folder_watch as fw
        cfg = load_config().get("folder_watch") or {}
        hour = int(cfg.get("daily_hour", 4))
        scan_min = int(cfg.get("scan_interval_min", 120))
        LOG.info("后台日任务就绪（每 %d 分钟扫描入队；生产=每日 %02d:00 "
                 "或开机补跑，滚动 24h 一次，待处理录像>%.0fGB 触发"
                 "（没处理过的都算）；未配置目录则等待选择）",
                 scan_min, hour, float(cfg.get("threshold_gb", 2)))
        boot = {"done": False}
        next_scan = 0.0
        while not self.daily_stop.is_set():
            wdir = (load_config().get("folder_watch") or {}).get(
                "watch_dir") or self.watch_dir
            if wdir:
                if time.time() >= next_scan:
                    try:
                        new = fw.scan(wdir)
                        if new:
                            LOG.info("扫描入队：%d 个新目标", new)
                        # 顺带回收陈旧租约(2026-10-27):宿主被杀留下的
                        # 「running」任务原本要卡到下一生产窗口才回收——
                        # 用户整晚看着「处理中」。扫描拍内回收,2h 内出结果。
                        if fw.expire_leases():
                            LOG.info("已回收超时租约（详见队列状态）")
                            self._ui(self._prod_refresh)
                    except Exception as e:          # noqa: BLE001
                        LOG.warning("扫描失败：%s", e)
                    next_scan = time.time() + scan_min * 60
                    try:
                        fw.mark_scan(next_scan)
                    except Exception:               # noqa: BLE001
                        pass
                due, why = fw.production_due(hour, boot=not boot["done"])
                if due:
                    boot["done"] = True
                    gate = self._bind_gate()
                    if gate:
                        LOG.warning("生产暂缓：%s", gate)
                        self._ui(lambda g=gate: self._flash(
                            f"生产前置未满足：{g}", BRAND["warn"]))
                    else:
                        added = fw.unprocessed_bytes(wdir)
                        limit = fw.threshold_bytes()
                        if added > limit:
                            LOG.info("生产开始（%s）：待处理录像 %.2fGB > "
                                     "%.0fGB（未处理都算）", why, added / 1e9,
                                     limit / 1e9)
                            self._ui(lambda: self._tray_tip("生产中"))
                            try:
                                n_ok, n_fail = self._daily_pass(wdir)
                                LOG.info("生产完成：成功 %d / 失败 %d",
                                         n_ok, n_fail)
                                if n_ok or n_fail:
                                    self._ui(lambda ok=n_ok, fl=n_fail:
                                             self._flash(
                                                 f"本机处理完成：成功{ok} "
                                                 f"失败{fl}", BRAND["warn"]
                                                 if fl else BRAND["dim"]))
                            except Exception as e:  # noqa: BLE001
                                LOG.warning("生产失败：%s", e)
                        else:
                            LOG.info("生产跳过（%s）：待处理录像 %.2fGB ≤ "
                                     "%.0fGB（未处理都算），当日权益照算", why,
                                     added / 1e9, limit / 1e9)
                        fw.mark_production_run()
                    self._ui(self._prod_refresh)
            self.daily_stop.wait(timeout=600)

    def _ui(self, fn):
        """后台线程 → 主线程;窗口已关(mainloop 退出)时静默丢弃。

        日任务/录制/更新等常驻线程在窗口关闭瞬间仍可能执行到回调,
        Tk 会抛 RuntimeError("main thread is not in main loop")并打印
        线程栈(测试 teardown 与实机关窗都会污染日志)。此处统一吞掉。
        """
        try:
            self.after(0, fn)
        except (tk.TclError, RuntimeError):
            pass

    def _bind_gate(self):
        api, prod = self._acct_cfg()
        if not api:
            return ""
        from toolbox.account import AccountClient
        try:
            pre = AccountClient(api, prod).prerequisites()
        except Exception:                        # noqa: BLE001
            return ""
        if pre.get("logged_in") and pre.get("binding") == "none":
            return "微信还没绑定（在「账号设置」里绑定后，今天就帮你补上）"
        return ""

    def _daily_pass(self, wdir):
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
                    self._watch_produce(session)
        return n_ok, n_fail

    def _watch_produce(self, session):
        """跟踪云端成片(REQ-20261021-005/REQ-20261022-008)。"""
        import toolbox.folder_watch as fw
        from toolbox import uploader as _up
        cfg = (load_config().get("server") or {})
        if not cfg.get("upload_url") or cfg.get("upload_token"):
            return
        try:
            from toolbox.transfer import discover_session
            disc = discover_session(session)
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
        except Exception as e:                      # noqa: BLE001
            LOG.info("成片跟踪跳过（%s 场）：%s", session, e)
            return
        api_prod = cfg["upload_url"]
        self._produce[session] = {"state": "waiting", "ts": time.time()}

        def poll():
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
                self._produce.pop(session, None)
                return

            def on_state(s):
                st = (s or {}).get("state")
                if st in ("waiting", "processing", "done", "failed"):
                    self._produce[session] = dict(
                        s or {}, ts=time.time())
                LOG.info("成片状态（%s）：%s", session, st)

            result = up2.wait_produce(upload_id, on_state=on_state)
            state = (result or {}).get("state")
            if state == "done":
                works = (result or {}).get("works_url", "")
                self._produce[session] = {"state": "done",
                                          "works_url": works,
                                          "ts": time.time()}
                self._save_last_works(session, works)
                from toolbox.uploader import works_full_url
                url = works_full_url(api_prod, works)
                LOG.info("成片完成（%s）", url)
                self.after(0, lambda: (
                    self._flash("成片完成！3 条候选已就绪，去「我的作品」"
                                "打开作品页挑选", BRAND["good"], sticky=True),
                    self._tray_tip("成片完成"), self._prod_refresh()))
            elif state == "failed":
                err = (result or {}).get("error") or ""
                self._produce[session] = {"state": "failed", "error": err,
                                          "ts": time.time()}
                LOG.warning("成片生成失败（%s）：%s", session, err[-300:])
                self.after(0, lambda: (self._flash(
                    "云端生成失败，可在「账号设置」复制诊断信息发给大师姐",
                    BRAND["danger"], sticky=True), self._prod_refresh()))
            else:
                self._produce[session] = {"state": "timeout", "ts": time.time()}
                self.after(0, lambda: (self._flash(
                    "云端还没生成完，稍后可在「我的作品」打开作品页查看",
                    BRAND["warn"], sticky=True), self._prod_refresh()))

        threading.Thread(target=poll, daemon=True,
                         name=f"produce-{session[:12]}").start()

    # ---------- 托盘 ----------

    def _on_unmap(self, _e):
        if self.wm_state() == "iconic":
            self.after(120, self._to_tray)

    def _to_tray(self):
        if self.wm_state() != "iconic":
            return
        self.withdraw()
        if self.tray is None:
            self._tray_init()
        if self.tray is not None:
            self._tray_tip(self._status_text or "待命")

    def _tray_init(self):
        try:
            import pystray
            from PIL import Image, ImageDraw
        except ImportError:
            LOG.info("未装 pystray，最小化停留在任务栏")
            return
        img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
        d = ImageDraw.Draw(img)
        d.rounded_rectangle([4, 4, 60, 60], radius=14, fill=BRAND["primary"])
        d.polygon([(26, 20), (26, 44), (48, 32)], fill="#FFFFFF")
        menu = pystray.Menu(
            pystray.MenuItem("显示主界面", self._tray_act_show, default=True),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("退出", self._tray_act_quit),
        )
        self.tray = pystray.Icon("dashijie", img, version.PRODUCT, menu)
        try:
            self.tray.run_detached()
        except Exception as e:                   # noqa: BLE001
            LOG.warning("托盘启动失败：%s", e)
            self.tray = None

    def _tray_act_show(self, *_):
        try:
            self.after(0, self._tray_show)
        except (tk.TclError, RuntimeError):
            pass

    def _tray_act_quit(self, *_):
        try:
            self.after(0, self._on_close)
        except (tk.TclError, RuntimeError):
            pass

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
            except Exception:                    # noqa: BLE001
                pass

    def _tray_tip(self, text):
        if self.tray is not None:
            try:
                self.tray.title = f"{version.PRODUCT} · {text}"
            except Exception:                    # noqa: BLE001
                pass

    # ---------- 状态行 ----------

    def _show_status(self, text, color=None, sticky=False):
        self._status_text = text
        self._status_sticky = sticky
        self.status_lab.config(text=text, fg=color or BRAND["dim"])

    def _hide_status(self):
        self._status_sticky = False
        self._status_text = ""
        self.status_lab.config(text="")

    def _flash(self, text, color=None, sticky=False):
        self._show_status(text, color or BRAND["primary"], sticky=sticky)
        if not sticky:
            self.after(2500, lambda: self._hide_status()
                       if self._status_text == text else None)

    # ---------- 调试快照 ----------

    def _dev_snapshot(self):
        snapdir = os.environ.get("PET_SNAPSHOT")
        if not snapdir:
            return
        try:
            from PIL import ImageGrab
        except ImportError:
            return
        Path(snapdir).mkdir(parents=True, exist_ok=True)
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
        self._rec_auto_on = False          # 先停游戏监控
        self._auto_wake.set()
        if self._rec is not None:          # 退出前先收尾录制（写完 moov）
            try:
                self._rec.stop(timeout=25)
            except Exception as e:         # noqa: BLE001
                LOG.warning("退出停录异常：%s", e)
            self._rec = None
        if self.daily_thread is not None and self.daily_thread.is_alive():
            import toolbox.folder_watch as fw
            self.daily_stop.set()
            fw.kill_current()
        self._save_hist()
        self._tray_stop()
        self.destroy()


def run(address=""):
    """GUI 入口。DPI 在 PetWindow.__init__ 内、建窗之前声明。"""
    dpi.setup()
    PetWindow(address=address).mainloop()
