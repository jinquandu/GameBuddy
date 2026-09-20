"""逐帧行为识别：对视频采样帧做 OCR，识别 5 类游戏事件并产出带时间戳的事件流。

识别目标（基于 480p 三角洲行动直播实测校准）：
- match_start 进入对局：部署期底部字幕「指挥官：我们即将着陆…搜寻高价值
  物资…」（cy>0.75H，持续 10-15s）
- down     击倒：中央下方击杀信息流「击倒+玩家名」（cy≈0.72H，持续 2-4s）
- loot     拾取：容器计数（背包/口袋/安全箱 N/M）增加即拾取事件；
           估价信号按优先级：拾取窗文本匹配价格表 > 语音交叉（--transcript，
           ±10s 高价值词，480p 下最可靠）> 品质色块（默认关，见下）
- extract  撤离：结算大字「撤离成功/撤离失败」（cy≈0.46H）；命中帧追加全屏
           OCR 快照（撤离点/用时；实测 480p 结算面板无可读收益清单，故收益
           解析为尽力而为）
- dance    跳舞：HUD 动作提示关键词（本场直播无样本，规则可配置，未实测）

品质色块检测（detector.quality_color_enabled，默认 false）：容器界面打开时
对物品格边框环采色统计红/金品质格。480p 直播源实测信噪比不足（对照帧与界面
帧的暖色像素相当，2026-09-08），仅建议在 1080p 录屏源下校准阈值后启用。

产出：JSONL 事件流，每行一个事件（kind/t_start/t_end/detail/confidence/meta）。
"""
import json
import os
import re
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import List, Optional

import cv2
import numpy as np

# OCR 引惰性导入：仅 detect 命令需要，避免拖慢其他子命令
_OCR = None


def _get_ocr():
    global _OCR
    if _OCR is None:
        # CUDA 支持为可选开关（默认关）。2026-09-18 Windows/RTX3070Ti 同素材
        # A/B 实测：P2 分片 CPU 245s vs CUDA 350s——OCR 模型小、SCAN_BAND 裁剪
        # 后单帧推理本就只占零点几秒，GPU 启动/拷贝开销反而倒挂；detect 的大
        # 头是视频解码（纯 CPU），真正有效的提速是 --workers 进程级并行。
        # 开启需装 onnxruntime-gpu（CUDA12 版）+ CUDA 版 torch，CUDA DLL 借道
        # torch/lib（os.add_dll_directory 免装 CUDA Toolkit）。
        use_cuda = False
        pref = os.environ.get("TOOLBOX_OCR_CUDA")   # 临时开关（benchmark 等）
        if pref is None:
            from toolbox.config import load_config
            pref = (load_config().get("detector") or {}).get("ocr_use_cuda")
        if pref is not None and str(pref).lower() not in ("0", "false", "no"):
            try:
                import torch
                if torch.cuda.is_available():
                    os.add_dll_directory(str(Path(torch.__file__).parent / "lib"))
                    import onnxruntime as ort
                    use_cuda = "CUDAExecutionProvider" in \
                        ort.get_available_providers()
            except Exception:
                use_cuda = False
        from rapidocr_onnxruntime import RapidOCR
        kw = ({"det_use_cuda": True, "cls_use_cuda": True, "rec_use_cuda": True}
              if use_cuda else {})
        _OCR = RapidOCR(**kw)
        print(f"  [OCR] {'CUDA' if use_cuda else 'CPU'} 模式", flush=True)
    return _OCR


# ---- 价格表：静态参考价（哈夫币），随市场波动，config.detector.price_table 可覆盖 ----
PRICE_TABLE = {
    "曼德尔砖": 3_500_000,
    "大红": 3_500_000,
    "红卡": 1_200_000,
    "金条": 800_000,
    "比特币": 600_000,
    "显卡": 450_000,
    "金卡": 400_000,
    "处理器": 300_000,
    "CPU": 300_000,
    "钥匙卡": 250_000,
}
QUALITY_FALLBACK = {  # 品质关键词 → 区间中值（识别不到具体物品时）
    "红色": 1_500_000, "金色": 400_000, "稀有": 150_000,
    "个红": 1_500_000, "出红": 1_500_000,   # "开了个红/出红了"口癖
}

DANCE_KEYWORDS = ["跳舞", "舞动", "热舞", "鬼步"]          # 待实测校准
CONTAINER_RE = re.compile(r"(背包|口袋|安全箱)[：:]\s*(\d+)\s*/\s*(\d+)")
DOWN_RE = re.compile(r"(击倒|击杀|淘汰|击毙|[EÉeé]limin)")   # Elimin=法语客户端播报
TARGET_RE = re.compile(r"(?:击倒|击杀|淘汰|击毙|[EÉeé]limin[ée]?\s*[:：]?)\s*"
                       r"([\w\u4e00-\u9fff·\-]+)")
# 撤离结算面板中央的「淘汰者/处决者」标签（谁淘汰了你）：落在击杀信息流
# 检测带内，TARGET_RE 会把「淘汰」当动词、「者」当玩家名——整条文本只有
# 动词+者、没有真实 ID 的，一律不是击杀信息流（2026-09-13 香菜大帝实测）
LABEL_ONLY_RE = re.compile(r"^(?:击倒|击杀|淘汰|击毙|处决)(?:者)?$")
EXTRACT_RE = re.compile(r"(撤离成功|撤离失败|任务失败|行动失败|行动成功)")
UI_OPEN_RE = re.compile(r"丢弃|拿取|拆分|装备对比|按住.{0,4}(拾取|拿)")
MATCH_RE = re.compile(r"即将着|牢记接下来|高价值物资")   # 部署期字幕（底部带）
LOOT_VOICE_RE = re.compile(r"出金|出红|开了个红|大红|曼德尔|金条|比特币|显卡|"
                           r"金卡|红卡|钥匙卡|血赚|毕业了?|这把肥")
DURATION_RE = re.compile(r"(\d{1,2}[:：]\d{2}[:：]\d{2})")
PROFIT_RE = re.compile(r"(?:哈夫币|收益|获得)[^\d]{0,6}([\d,，.]+)")
# 结算面板"本局收获"槽位（1080p 实测 cx≈0.21-0.26, cy≈0.47）：标签常被 OCR 漏读，
# 直接配对独立的百万分位数字；"击败干员N"（N 含中文数字）为游戏官方击杀口径，
# 比击杀信息流的全队击倒更准
PROFIT_NUM_RE = re.compile(r"^\d{1,3}(?:[,，]\d{3})+$")
KILLS_OFFICIAL_RE = re.compile(r"击败干员([一二三四五六七八九十\d]+)")

# 信息带（相对坐标 x0,y0,x1,y1），2026-09-09 跨 480p/1080p/中法语客户端实测校准，
# 详见 docs/plans/ocr-position-and-crop-study.md：击倒播报恒在 (0.50, 0.72-0.75) 且
# 框宽≤0.12；撤离结算大字 (0.45, 0.42)；部署字幕 cy>0.75；自己容器列 cx≈0.41
# （右列 0.69 是装备对比的对方容器，刻意裁掉，避免同名计数在两列间来回跳）。
# 带外（左侧聊天/小地图、顶部罗盘、右缘广告与摄像头）不含目标元素，裁掉后
# 检测网输入面积缩小约 3/4，单帧耗时约省 30-40%。
SCAN_BAND = (0.35, 0.28, 0.64, 0.88)

# HUD 常驻/广告词，从拾取上下文里剔除
_NOISE = re.compile(r"PeRo|俱乐部|kook|加速|专线|电竞|冠军|福利|G-COIN|接待|明星|招牌|"
                    r"三角洲|跑刀|pubg|显示装备|标记|Esc|前往此处|千克|等待救援|你好")


@dataclass
class DetectedEvent:
    kind: str                 # down / loot / extract / dance
    t_start: float            # 秒
    t_end: float
    detail: str
    confidence: float
    meta: dict = field(default_factory=dict)
    price: Optional[int] = None
    price_source: Optional[str] = None   # table / quality / None

    def to_line(self, src=""):
        d = asdict(self)
        d["t_start"] = round(d["t_start"], 1)
        d["t_end"] = round(d["t_end"], 1)
        if d["price"] is None:
            d.pop("price"); d.pop("price_source")
        d["src"] = src
        return json.dumps(d, ensure_ascii=False)


def _center(box):
    ys = [p[1] for p in box]; xs = [p[0] for p in box]
    return sum(xs) / 4, sum(ys) / 4          # (cx, cy)


def _estimate_price(texts, price_table):
    """从时间窗文本里找物品名估价。返回 (price, source) 或 (None, None)。"""
    joined = " ".join(texts)
    for name, price in price_table.items():
        if name in joined:
            return price, "table"
    for qual, price in QUALITY_FALLBACK.items():
        if qual in joined:
            return price, "quality"
    return None, None


def _scan_range(path, t_lo, t_hi, fps, pack):
    """扫描 [t_lo, t_hi] 时间段，返回 raw 事件列表 [(t, kind, detail, conf, meta)]。

    时间用视频真实 PTS（CAP_PROP_POS_MSEC），规避 VFR 下按帧号推算的漂移；
    t_lo>0 时毫秒 seek 后顺序读。pack=(dance_kw, quality_on, qgrid, price_table)。
    """
    dance_kw, quality_on, qgrid, price_table = pack
    ocr = _get_ocr()
    cap = cv2.VideoCapture(str(path))
    if not cap.isOpened():
        raise RuntimeError(f"无法打开视频: {path}")
    if t_lo > 0:
        cap.set(cv2.CAP_PROP_POS_MSEC, int(t_lo * 1000))
    fps_v = cap.get(cv2.CAP_PROP_FPS) or 25
    step = max(1, int(round(fps_v / fps)))

    containers = {}      # name -> last count（跨局重置时自然归零）
    pendings = {}        # name -> (t, detail, conf, meta) 待下一帧确认的拾取
    raw = []             # (t, kind, detail, conf, meta)
    last_snap_t = -1e9   # 撤离结算快照去抖（结算跟踪模式外）
    settle_until = -1e9  # 结算跟踪模式截止时刻：面板在信息带外（屏幕左侧），需全帧 OCR
    n = 0
    # 交替解码+OCR（单帧驻留）：帧列表驻留会把 OCR 吞吐拖慢 4-9 倍
    # （numpy/onnx 分配器碎片化，实测 488ms/帧 vs 驻留 1877ms/帧），勿改回
    # 跳帧取帧（2026-09-20）：grab() 只解码不做 YUV→BGR 转换拷贝，采样帧才
    # retrieve() 取 BGR——1080p60 实测 read() 241fps vs grab() 1430fps，解码
    # 耗时占比从 ~50% 降到 ~10%，且解码线程与 ORT 推理线程的争抢大幅减轻
    # （此前单分片 30min 素材要跑 ~30min，改后 ~9min）。POS_MSEC 取的是刚
    # grab 帧的 PTS，时间语义与原 read() 版完全一致
    while True:
        if not cap.grab():
            break
        t = cap.get(cv2.CAP_PROP_POS_MSEC) / 1000.0
        if t > t_hi:
            break
        if n % step == 0:
            ok, frame = cap.retrieve()
            if not ok:
                n += 1
                continue
            h, w = frame.shape[:2]
            full_mode = t < settle_until
            if full_mode:
                # 结算跟踪：撤离面板（撤离成功大字 (0.18,0.23)、本局收获数字
                # (0.21-0.26,0.47)）在 SCAN_BAND 左侧，动画结束后带内无触发
                res, _ = ocr(frame)
                items = []
                if res:
                    for box, text, score in res:
                        cx, cy = _center(box)
                        items.append((cx, cy, text, score))
            else:
                bx0, by0, bx1, by1 = SCAN_BAND
                roi = frame[int(h * by0):int(h * by1), int(w * bx0):int(w * bx1)]
                res, _ = ocr(roi)
                items = []
                if res:
                    for box, text, score in res:
                        cx, cy = _center(box)
                        # 裁剪带内坐标映射回全帧坐标，下游位置过滤逻辑保持不变
                        items.append((cx + w * bx0, cy + h * by0, text, score))

            texts_all = [it[2] for it in items]
            # 撤离结算帧不检测击倒：击杀信息流不可能与结算面板同屏
            # （「淘汰者」标签与击杀者名字可能被 OCR 连读，文本过滤兜不住）
            settled = any(EXTRACT_RE.search(x) for x in texts_all)
            # 1) 击倒信息流：中央水平带
            if not settled:
                for cx, cy, text, score in items:
                    if abs(cx - w / 2) < w * 0.35 and h * 0.62 < cy < h * 0.82 \
                            and DOWN_RE.search(text) and not LABEL_ONLY_RE.match(text):
                        m = TARGET_RE.search(text)
                        target = m.group(1) if m else ""
                        raw.append((t, "down", f"击倒 {target}".strip(),
                                    float(score), {"target": target, "text": text}))
            # 1b) 进入对局：部署期底部字幕带（实测持续 10-15s，多帧稳定可读）
            for cx, cy, text, score in items:
                if cy > h * 0.75 and MATCH_RE.search(text):
                    raw.append((t, "match_start", "进入对局（部署）",
                                float(score), {"text": text}))
            # 2) 撤离结算：命中即进入结算跟踪模式（settle_until）。收益面板在
            #    "撤离成功"大字后约 5s 弹出且位于信息带外，跟踪期改用全帧 OCR，
            #    快照每采样帧附带（聚合层做并集去重）；结算每场仅 ~12 次，
            #    全帧 OCR 增量约 15s/场，可接受
            for cx, cy, text, score in items:
                if EXTRACT_RE.search(text):
                    settle_until = t + 12.0
                    point = next((x for x in texts_all if "撤离点" in x), "")
                    meta_e = {"point": point}
                    if full_mode:
                        snap = [t2 for _, t2, s2 in (res or []) if s2 > 0.6]
                        last_snap_t = t
                    elif t - last_snap_t > 4.5:
                        last_snap_t = t
                        full_res, _ = ocr(frame)
                        snap = [t2 for _, t2, s2 in (full_res or []) if s2 > 0.6]
                    else:
                        snap = []
                    if snap:
                        meta_e["snapshot"] = snap[:30]
                    raw.append((t, "extract", text, float(score), meta_e))
            # 3) 跳舞（关键词提示）
            for cx, cy, text, score in items:
                if any(k in text for k in dance_kw) and not _NOISE.search(text):
                    raw.append((t, "dance", text, float(score), {}))
            # 3b) 品质色块（默认关闭）：界面打开时对物品格边框环采色。
            #     480p 实测信噪比不足，仅 1080p 源校准 quality_grid 后启用。
            if quality_on and any(UI_OPEN_RE.search(x) for x in texts_all):
                q = _quality_grid(frame, qgrid)
                if q["红"] or q["金"]:
                    price, psrc = _estimate_price(
                        [f"{'红色' if q['红'] else '金色'}"], price_table)
                    raw.append((t, "loot",
                                f"界面品质格 红{q['红']} 金{q['金']}", 0.5,
                                {"quality": q, "price": price,
                                 "price_source": psrc}))
            # 4) 拾取：容器计数增加（连续确认制——计数增加后下一采样帧仍
            #    保持才记事件，过滤观战视角切换造成的计数来回抖动）
            for cx, cy, text, score in items:
                m = CONTAINER_RE.search(text)
                if not m:
                    continue
                name, cur = m.group(1), int(m.group(2))
                prev = containers.get(name)
                containers[name] = cur
                if prev is not None and cur > prev:
                    ctx = [x for x in texts_all
                           if not _NOISE.search(x) and not CONTAINER_RE.search(x)]
                    price, psrc = _estimate_price(ctx, price_table)
                    meta_d = {"container": name, "prev": prev, "cur": cur,
                              "context": ctx[:6]}
                    if price:
                        meta_d["price"], meta_d["price_source"] = price, psrc
                    pendings[name] = (t, f"{name} {prev}->{cur}",
                                      float(score), meta_d)
                elif name in pendings and cur >= pendings[name][3].get("cur", 0):
                    pt, detail, pscore, meta_d = pendings.pop(name)
                    raw.append((pt, "loot", detail, pscore, meta_d))
                elif name in pendings:
                    pendings.pop(name)      # 计数回落 = 视角抖动，丢弃
        n += 1
    return raw


def _video_duration(path):
    """ffprobe 读视频时长（秒）。"""
    import subprocess
    r = subprocess.run(["ffprobe", "-v", "error", "-show_entries",
                        "format=duration", "-of", "csv=p=0", str(path)],
                       capture_output=True, text=True)
    return float(r.stdout.strip())


def scan_video(path, fps=1.0, config=None, progress=None, transcript=None,
               workers=1):
    """扫描视频，返回按时间排序的事件列表。

    只 OCR 信息带 SCAN_BAND（目标元素相对位置跨源固定，实测见
    docs/plans/ocr-position-and-crop-study.md）。
    transcript：可选 [{start,end,text}]（ASR 句级转写），用于拾取估价语音交叉。
    workers>1 时按时间段分段并行（每进程独立 OCR，单帧推理本就是单核瓶颈，
    进程级并行是本机唯一有效加速；段间重叠 2s 保证边界事件不丢）。
    """
    cfg = (config or {}).get("detector") or {}   # 裸键（子项全注释）YAML 解析为 None
    price_table = dict(PRICE_TABLE)
    price_table.update(cfg.get("price_table", {}))
    pack = (cfg.get("dance_keywords", DANCE_KEYWORDS),
            bool(cfg.get("quality_color_enabled", False)),
            cfg.get("quality_grid", {}),
            price_table)
    merge_gap = 2.5      # 采样秒，同类同详情事件在此间隔内合并

    if workers and workers > 1:
        # 子进程分段并行：multiprocessing.Pool 在 macOS 上 spawn 死锁、fork 后
        # cv2/AVFoundation 打不开视频（均实测），独立进程是可靠路径
        import os
        import shutil
        import subprocess
        import sys
        import tempfile
        from toolbox.config import ROOT
        dur = _video_duration(path)
        seg = dur / workers
        pack_json = json.dumps(list(pack))
        tmpdir = tempfile.mkdtemp(prefix="toolbox_scan_")
        env = dict(os.environ, PYTHONPATH=str(ROOT))
        procs, outs = [], []
        try:
            for i in range(workers):
                # 段间重叠 2s：边界事件多帧冗余，聚合阶段自然去重
                t_lo, t_hi = max(0.0, i * seg - 2), (i + 1) * seg
                out_json = os.path.join(tmpdir, f"part{i}.json")
                cmd = [sys.executable, "-m", "toolbox._scan_worker",
                       str(path), f"{t_lo}", f"{t_hi}", f"{fps}",
                       pack_json, out_json]
                procs.append((subprocess.Popen(cmd, env=env), out_json))
            raw = []
            for p, o in procs:
                if p.wait() != 0:
                    raise RuntimeError(f"扫描 worker 失败 (exit {p.returncode})")
                with open(o, encoding="utf-8") as f:
                    raw.extend(json.load(f))
            raw.sort(key=lambda r: r[0])
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)
    else:
        raw = _scan_range(str(path), 0, 1e9, fps, pack)
        if progress:
            progress(t=len(raw) and raw[-1][0] or 0)

    events = _aggregate(raw, merge_gap)
    _apply_voice(events, transcript, price_table)
    return events


def _apply_voice(events, transcript, price_table):
    """拾取估价语音交叉 + 独立触发（详见类 docstring）。

    句子若带 speaker 字段（toolbox speaker 产出），只认主播句——TTS 播报
    （如"检测到大量数据处理器"含估价词）不参与语音估价。
    """
    if not transcript:
        return
    transcript = [s for s in transcript if s.get("speaker", "host") == "host"]
    for e in events:
        if e.kind != "loot" or e.price:
            continue
        mid = (e.t_start + e.t_end) / 2
        hints = [s["text"] for s in transcript
                 if s["start"] - 10 <= mid <= s["end"] + 10
                 and LOOT_VOICE_RE.search(s["text"])]
        if hints:
            e.meta["voice_hint"] = hints[:3]
            price, src = _estimate_price(hints, price_table)
            if price:
                e.price, e.price_source = price, src
                e.detail += "（语音高价值）"
    # 语音独立触发：高价值口癖但 ±10s 无视觉拾取事件（容器计数漏读时兜底）。
    # 仅认 ≤40 字的短反应句——摸到好货是短惊呼，长句必是聊天/唱歌叙事。
    vis = [e for e in events if e.kind == "loot"]
    extra = []
    for s in transcript:
        if len(s["text"]) > 40 or not LOOT_VOICE_RE.search(s["text"]):
            continue
        mid = (s["start"] + s["end"]) / 2
        if any(e.t_start - 10 <= mid <= e.t_end + 10 for e in vis):
            continue
        price, src = _estimate_price([s["text"]], price_table)
        extra.append(DetectedEvent(
            "loot", s["start"], s["end"],
            f"语音高价值: {s['text'][:30]}", 0.7,
            {"voice_hint": [s["text"]]}, price, src))
    events.extend(extra)
    events.sort(key=lambda e: e.t_start)


def _quality_grid(frame, qgrid):
    """容器界面物品格边框环采色，返回 {'红':n,'金':n}。阈值面向 1080p 校准。"""
    hsv = cv2.cvtColor(frame, cv2.COLOR_BGR2HSV)
    h, w = hsv.shape[:2]
    x0 = int(w * qgrid.get("x0", 0.08)); x1 = int(w * qgrid.get("x1", 0.62))
    y0 = int(h * qgrid.get("y0", 0.18)); y1 = int(h * qgrid.get("y1", 0.78))
    cols = int(qgrid.get("cols", 7)); rows = int(qgrid.get("rows", 5))
    gw, gh = (x1 - x0) / cols, (y1 - y0) / rows
    counts = {"红": 0, "金": 0}
    for r in range(rows):
        for c in range(cols):
            gx0, gx1 = int(x0 + c * gw + gw * 0.15), int(x0 + (c + 1) * gw - gw * 0.15)
            gy0, gy1 = int(y0 + r * gh + gh * 0.15), int(y0 + (r + 1) * gh - gh * 0.15)
            if gx1 - gx0 < 6 or gy1 - gy0 < 6:
                continue
            ring = np.concatenate([
                hsv[gy0:gy0 + 3, gx0:gx1].reshape(-1, 3),
                hsv[gy1 - 3:gy1, gx0:gx1].reshape(-1, 3),
                hsv[gy0:gy1, gx0:gx0 + 3].reshape(-1, 3),
                hsv[gy0:gy1, gx1 - 3:gx1].reshape(-1, 3)])
            hm = np.median(ring[:, 0]); sm = np.median(ring[:, 1]); vm = np.median(ring[:, 2])
            if sm > 80 and vm > 60:
                if hm < 9 or hm > 171:
                    counts["红"] += 1
                elif 10 <= hm <= 30:
                    counts["金"] += 1
    return counts


def _edit_le1(a, b):
    """编辑距离是否 ≤1（OCR 抖动容忍，如"雪代巴dd"vs"雪代dd"）。"""
    if a == b:
        return True
    if abs(len(a) - len(b)) > 1:
        return False
    if len(a) == len(b):
        return sum(x != y for x, y in zip(a, b)) <= 1
    if len(a) > len(b):
        a, b = b, a
    i = j = diff = 0
    while i < len(a) and j < len(b):
        if a[i] == b[j]:
            i += 1; j += 1
        else:
            j += 1; diff += 1
            if diff > 1:
                return False
    return True


def _same_target(a, b):
    """两次击倒读数是否同一目标（击杀信息流重读抖动容忍）。

    信息流停留 2~4s 被 1fps 采样多次，目标名常见抖动形态：截断（Z ⊂ Zzco）、
    字符增删（岭南人爱少/岭南源少）。骨架规则（首2字+末字同、长度差≤1）仅对
    ≥3 字名生效，避免短名误并（如 Z vs 路铃）。
    """
    if a == b:
        return True
    if not a or not b:
        return False
    if _edit_le1(a, b):
        return True
    lo, hi = (a, b) if len(a) <= len(b) else (b, a)
    if lo in hi:                                            # 截断重读
        return True
    if len(lo) >= 3 and a[:2] == b[:2] and a[-1] == b[-1] \
            and abs(len(a) - len(b)) <= 1:                  # 骨架一致
        return True
    return False


def _aggregate(raw, gap):
    """相邻同类采样点合并为一个事件；down 的目标名做模糊匹配，取置信度高的文本。

    down：击杀信息流停留 2~4s，1fps 采样常把同一次击倒读到多次且目标名抖动
    （Zzco/Z），向前 4s 回溯找最近一次 down（可隔 loot 等其他事件）合并，
    防止连杀数虚增。
    match_start 的部署字幕句间有空隙（可达 10s），用 20s 专属间隔避免一次
    部署被拆成多条。
    """
    events = []
    for t, kind, detail, conf, meta in sorted(raw, key=lambda r: r[0]):
        if kind == "down":
            target = meta.get("target", "")
            hit = None
            for e in reversed(events):
                if e.t_start < t - 4.0:      # 播报最长停留 ~4s
                    break
                if e.kind == "down" and _same_target(
                        e.meta.get("target", ""), target):
                    hit = e
                    break
            if hit is not None:
                if conf > hit.confidence:
                    hit.detail, hit.meta = detail, dict(meta)   # 保留更准的读数
                hit.t_end = t
                hit.confidence = max(hit.confidence, conf)
                continue
            events.append(DetectedEvent(kind, t, t, detail, conf, dict(meta)))
            continue
        if events:
            e = events[-1]
            kgap = 20.0 if kind == "match_start" else gap
            same = e.kind == kind and t - e.t_end <= kgap
            if same and kind == "extract":
                same = e.detail == detail
                if same:      # 结算快照并集（面板分阶段出现）
                    seen = set(e.meta.get("snapshot", []))
                    extra = [x for x in meta.get("snapshot", []) if x not in seen]
                    e.meta["snapshot"] = (e.meta.get("snapshot", []) + extra)[:40]
            elif same:
                same = e.detail == detail
            if same:
                e.t_end = t
                e.confidence = max(e.confidence, conf)
                continue
        events.append(DetectedEvent(kind, t, t, detail, conf, dict(meta)))
    for e in events:
        if e.kind == "loot" and e.meta.get("price"):
            e.price = e.meta.pop("price")
            e.price_source = e.meta.pop("price_source")
        if e.kind == "extract":
            snap = e.meta.get("snapshot", [])
            dur = max((DURATION_RE.search(x).group(1) for x in snap
                       if DURATION_RE.search(x)), default="")
            if dur:
                e.meta["duration"] = dur
            m = next((PROFIT_RE.search(x) for x in snap if PROFIT_RE.search(x)), None)
            if m is None:
                # "本局收获"标签漏读时的兜底：首个 ≥1000 的独立千分位数字
                m = next((PROFIT_NUM_RE.match(x) for x in snap
                          if PROFIT_NUM_RE.match(x)
                          and int(x.replace(",", "").replace("，", "")) >= 1000), None)
            if m:
                e.meta["profit"] = m.group(1) if m.lastindex else m.group(0)
            k = next((KILLS_OFFICIAL_RE.search(x) for x in snap
                      if KILLS_OFFICIAL_RE.search(x)), None)
            if k:
                e.meta["kills_official"] = k.group(1)
    return events


def write_jsonl(events, path, src=""):
    with open(path, "w", encoding="utf-8") as f:
        for e in events:
            f.write(e.to_line(src) + "\n")


def summarize(events):
    """控制台摘要行。"""
    from collections import Counter
    c = Counter(e.kind for e in events)
    total_price = sum(e.price or 0 for e in events if e.kind == "loot")
    parts = [f"{k}={v}" for k, v in sorted(c.items())] or ["无事件"]
    s = "事件: " + ", ".join(parts)
    if total_price:
        s += f" | 拾取估价合计≈{total_price:,}"
    return s
