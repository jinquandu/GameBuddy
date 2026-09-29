"""逐帧行为识别：对视频采样帧做 OCR，识别 6 类游戏事件并产出带时间戳的事件流。

识别目标（基于 480p/1080p 三角洲行动直播实测校准）：
- match_start 进入对局：部署期底部字幕「指挥官：我们即将着陆…搜寻高价值
  物资…」（cy>0.75H，持续 10-15s）
- loadout  入局装备：围绕 match_start 的两个窗口补扫——大厅窗（字幕前
  150s）读左下书法体干员名卡与顶部「出战干员切换为X」播报条定干员，
  职业经 OPERATOR_CLASS 静态表映射；落地窗（字幕后 30-150s）读局内 HUD
  右下枪械名（如 SCAR-H），首个稳定读数为主武器，类别经 WEAPON_TYPE 表
  映射（2026-09-20 1080p 实测定位，详见 _scan_loadout docstring）
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
_BATCH_OCR = None
_BATCH_TRIED = False


def _get_batch_ocr():
    """GPU 批量 OCR 引擎（TOOLBOX_OCR_BATCH=1 时启用，配合 GPU worker）。
    批量推理摊薄单帧 H2D/同步开销（实测单 worker ~2.7 -> ~10 帧/s）；
    初始化失败自动回退逐帧路径。"""
    global _BATCH_OCR, _BATCH_TRIED
    if not os.environ.get("TOOLBOX_OCR_BATCH"):
        return None
    if not _BATCH_TRIED:
        _BATCH_TRIED = True
        try:
            from toolbox.ocr_gpu import GpuOCR
            kw = {}
            if os.environ.get("TOOLBOX_OCR_DET_LIMIT"):
                kw["det_limit_side_len"] = int(os.environ["TOOLBOX_OCR_DET_LIMIT"])
            if os.environ.get("TOOLBOX_OCR_DET_TYPE"):
                kw["det_limit_type"] = os.environ["TOOLBOX_OCR_DET_TYPE"]
            _BATCH_OCR = GpuOCR(**kw)
        except Exception as ex:
            print(f"  [OCR] GPU 批量引擎不可用（{ex}），回退逐帧", flush=True)
            _BATCH_OCR = None
    return _BATCH_OCR


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
        # 并行 worker 用 TOOLBOX_OCR_THREADS 限 ORT 线程池。OMP_NUM_THREADS 对
        # onnxruntime 线程池无效：默认每进程吃满全部核，N 进程互相拖慢——实测
        # 5 并发窄条 OCR 合计吞吐反而低于单进程；intra_op=2 后 5 并发吞吐翻倍
        n_thr = os.environ.get("TOOLBOX_OCR_THREADS")
        if n_thr and n_thr.isdigit() and int(n_thr) >= 1:
            kw.update(intra_op_num_threads=int(n_thr), inter_op_num_threads=1)
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

# ---- 入局装备（loadout）：干员→职业、枪械→类别静态表 ----
# 干员池随赛季更新（截至 2026-09 S11 群星赛季）；config.detector.operator_class
# 可覆盖/扩充（含 OCR 别名，如 乌鲁鲁/乌鲁）。干员职业固定，读到名字即可查表。
OPERATOR_CLASS = {
    "红狼": "突击", "威龙": "突击", "无名": "突击", "疾风": "突击",
    "蜂医": "支援", "蛊": "支援",
    "牧羊人": "工程", "泰瑞": "工程", "乌鲁鲁": "工程", "乌鲁": "工程",
    "深蓝": "工程",
    "露娜": "侦察", "骇爪": "侦察", "麦晓雯": "侦察",
}
# 常见枪械→类别（烽火地带，未收录的返回"未分类"但保留原名；
# config.detector.weapon_type 可覆盖/扩充）
WEAPON_TYPE = {
    # 突击步枪
    "M4A1": "突击步枪", "M16A4": "突击步枪", "CAR-15": "突击步枪",
    "AKM": "突击步枪", "AK-12": "突击步枪", "SCAR-H": "突击步枪",
    "AUG": "突击步枪", "G3": "突击步枪", "QBZ95": "突击步枪",
    "QBZ95-1": "突击步枪", "K437": "突击步枪", "AS Val": "突击步枪",
    "ASVal": "突击步枪", "M7": "突击步枪", "PTR-32": "突击步枪",
    "K416": "突击步枪",
    "M14": "射手步枪",                      # M14 系按半自动射手步枪口径
    # 冲锋枪
    "MP5": "冲锋枪", "UZI": "冲锋枪", "乌兹": "冲锋枪",
    "Vector": "冲锋枪", "维克托": "冲锋枪", "野牛": "冲锋枪",
    "PP-19": "冲锋枪", "P90": "冲锋枪", "MP7": "冲锋枪",
    "SMG-45": "冲锋枪",
    # 射手步枪 / 狙击枪
    "SR-25": "射手步枪", "MK14": "射手步枪", "MK-14": "射手步枪",
    "Mini14": "射手步枪", "Mini-14": "射手步枪", "SKS": "射手步枪",
    "SVD": "狙击枪", "SVU": "狙击枪", "M700": "狙击枪", "R93": "狙击枪",
    "AWM": "狙击枪", "PSG-1": "狙击枪",
    # 霰弹枪
    "M870": "霰弹枪", "S12K": "霰弹枪", "M1014": "霰弹枪",
    # 轻机枪
    "M249": "轻机枪", "QJY201": "轻机枪", "QJY-201": "轻机枪",
    "PKM": "轻机枪", "M250": "轻机枪",
    # 手枪
    "93R": "手枪", "G17": "手枪", "G18C": "手枪", "M1911": "手枪",
    "沙漠之鹰": "手枪", "Glock17": "手枪",
    # 近战
    "坠星者": "近战", "唐刀": "近战",
}
# loadout 补扫 ROI（相对坐标 x0,y0,x1,y1）。2026-09-20 跨两位主播 1080p 实测：
# 大厅备战界面左下书法体干员名 (0.05-0.11, 0.73-0.80)，浅色皮肤字体可读
# （深色描金皮肤 OCR 不可读，此时依赖切换播报条）；顶部中央播报条
# 「出战干员切换为骇爪」(0.50, 0.16)；落地后局内自己武器名 (0.86-0.90,
# 0.935-0.945) 框高仅 14-16px——x>0.94 是右侧队友卡片（带队友枪名），
# 贴片贴纸字框高 70px+，均按框几何剔除；Tab 背包界面武器名+口径纵列
# (0.28-0.45, 0.38-0.46)（口径 9x19mm 为锚点）。
LOADOUT_ROI_OP = (0.02, 0.68, 0.22, 0.88)       # 干员名卡（含世界聊天/显示装备噪声）
LOADOUT_ROI_TOAST = (0.34, 0.10, 0.68, 0.22)    # 顶部播报条
LOADOUT_ROI_WEAPON = (0.80, 0.90, 0.93, 0.965)  # 局内 HUD 自己的武器名
LOADOUT_ROI_TAB = (0.24, 0.34, 0.52, 0.58)      # Tab 背包武器列（口径锚定）
OP_SWITCH_RE = re.compile(r"出战干员切换为([\u4e00-\u9fff]{2,6})")
AMMO_RE = re.compile(r"^[\d]{1,3}\s*[/／:：]\s*[\d]{1,3}$")   # HUD 弹药 30/120
CALIBER_RE = re.compile(r"\d+(?:\.\d+)?\s*[xX×]\s*\d{2,3}", re.A)   # 9x19mm 口径
TAB_MARK_RE = re.compile(r"(胸挂|口袋|背包|安全箱)\s*[：:]")       # 背包界面标志
# 枪械 ROI 内的非枪械噪声（HUD 状态词 / 界面控件 / 装备部件名）
_LOADOUT_NOISE = re.compile(
    r"全自动|半自动|单发|连发|点射|关闭|售出|上架|购买|出售|丢弃|拿取|"
    r"千克|显示装备|补齐队友|配装|准备|取消|头盔|背心|背包|口袋|安全箱|胸挂|"
    r"耐久|维修|消音|镜|握把|弹匣|口径|^\d+\s*(秒|%|米|分|个|格|级|星)")
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
# 分段并行的预热秒数：跨段状态（容器计数基线/拾取连续确认）最长需要约
# 2 个采样帧重建，12s 留足余量且成本仅 ~2.8%（10 段 × 12s / 4252s）
SEG_WARMUP = 12.0
# CUDA worker 相对 CPU worker 的吞吐权重（分段加权用）：cls 关闭后实测
# GPU worker ~3.3 帧/s vs 混部中 CPU worker ~1.2 帧/s，取 2.5 留余量
GPU_WORKER_WEIGHT = 4.0

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


def _scan_range(path, t_lo, t_hi, fps, pack, emit_from=0.0):
    """扫描 [t_lo, t_hi] 时间段，返回 raw 事件列表 [(t, kind, detail, conf, meta)]。

    时间用视频真实 PTS（CAP_PROP_POS_MSEC），规避 VFR 下按帧号推算的漂移；
    t_lo>0 时毫秒 seek 后顺序读。pack=(dance_kw, quality_on, qgrid, price_table,
    band_scale)；第 5 项可选——信息带降采样系数（0.8 实测 -35% 耗时、
    命中基本不丢；1.0=原尺度召回最大）。
    emit_from：分段并行时每段先向前多扫一段「预热」区间积累容器计数/待确认
    拾取等跨段状态，t<emit_from 的事件丢弃——边界零丢失且不重复。

    采样用绝对时间格点（整秒对齐，非「段内第 N 帧」取模）：分段 seek 的落点
    余量会让取模格点整体偏移 0~1s，击杀信息流文字只在停留期中段可读，
    格点偏移即漏帧（实测 87 -> 78 条）；整秒格点下分段与串行采样完全一致。
    """
    dance_kw, quality_on, qgrid, price_table = pack[0], pack[1], pack[2], pack[3]
    band_scale = pack[4] if len(pack) > 4 else 1.0
    ocr = _get_ocr()
    batch_ocr = _get_batch_ocr()      # GPU 批量引擎；不可用为 None（逐帧路径）
    cap = cv2.VideoCapture(str(path))
    if not cap.isOpened():
        raise RuntimeError(f"无法打开视频: {path}")
    if t_lo > 0:
        cap.set(cv2.CAP_PROP_POS_MSEC, int(t_lo * 1000))
    hop = 1.0 / fps
    next_t = (int(t_lo * fps) + 1) / fps    # 绝对时间格点（见 docstring）

    containers = {}      # name -> last count（跨局重置时自然归零）
    pendings = {}        # name -> (t, detail, conf, meta) 待下一帧确认的拾取
    raw = []             # (t, kind, detail, conf, meta)
    last_snap_t = -1e9   # 撤离结算快照去抖（结算跟踪模式外）
    settle_until = -1e9  # 结算跟踪模式截止时刻：面板在信息带外（屏幕左侧），需全帧 OCR

    def _ocr_one(img):
        """单图 OCR（批量引擎/CPU 引擎自适应），返回 res 列表。"""
        if batch_ocr is not None:
            return batch_ocr.run([img])[0]
        return ocr(img, use_cls=False)[0]

    def _apply(t, frame, res, items, full_mode):
        """单帧事件抽取（逐帧/批量两条 OCR 路径共用）。"""
        nonlocal settle_until, last_snap_t
        texts_all = [it[2] for it in items]
        # 撤离结算帧不检测击倒：击杀信息流不可能与结算面板同屏
        # （「淘汰者」标签与击杀者名字可能被 OCR 连读，文本过滤兜不住）
        settled = any(EXTRACT_RE.search(x) for x in texts_all)
        # 1) 击倒信息流：中央水平带
        if not settled:
            h, w = frame.shape[:2]
            for cx, cy, text, score in items:
                if abs(cx - w / 2) < w * 0.35 and h * 0.62 < cy < h * 0.82 \
                        and DOWN_RE.search(text) and not LABEL_ONLY_RE.match(text):
                    m = TARGET_RE.search(text)
                    target = m.group(1) if m else ""
                    raw.append((t, "down", f"击倒 {target}".strip(),
                                float(score), {"target": target, "text": text}))
        h, w = frame.shape[:2]
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
                    full_res = _ocr_one(frame)
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

    def _band_items(frame, roi):
        """信息带 ROI 的 OCR 结果映射回全帧坐标。"""
        bx0, by0, _, _ = SCAN_BAND
        h, w = frame.shape[:2]
        inv = 1.0 / band_scale
        res = _ocr_one(roi)
        items = []
        for box, text, score in (res or []):
            cx, cy = _center(box)
            items.append((cx * inv + w * bx0, cy * inv + h * by0, text, score))
        return res, items

    # GPU 批量缓冲：同形信息带帧整批过 det/rec（摊薄 H2D/同步开销）；
    # 结算全帧（形状不同）或引擎不可用时走逐帧路径
    buf = []      # [(t, roi, frame)]

    def _flush():
        if not buf:
            return
        results = batch_ocr.run([roi for _, roi, _ in buf])
        for (t, roi, frame), res in zip(buf, results):
            bx0, by0, _, _ = SCAN_BAND
            h, w = frame.shape[:2]
            inv = 1.0 / band_scale
            items = []
            for box, text, score in (res or []):
                cx, cy = _center(box)
                items.append((cx * inv + w * bx0, cy * inv + h * by0,
                              text, score))
            _apply(t, frame, res, items, full_mode=False)
        buf.clear()

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
        if t >= next_t:
            next_t += hop           # 从格点恒定步进；勿按采样帧 t 重推导——
            ok, frame = cap.retrieve()   # 容差+重推导会在每秒末尾连采 ~7 帧
            if not ok:
                continue
            h, w = frame.shape[:2]
            full_mode = t < settle_until
            if full_mode:
                # 结算跟踪：撤离面板（撤离成功大字 (0.18,0.23)、本局收获数字
                # (0.21-0.26,0.47)）在 SCAN_BAND 左侧，动画结束后带内无触发
                res = _ocr_one(frame)
                items = [(*_center(box), text, score)
                         for box, text, score in (res or [])]
                _apply(t, frame, res, items, full_mode=True)
            elif batch_ocr is not None:
                bx0, by0, bx1, by1 = SCAN_BAND
                roi = frame[int(h * by0):int(h * by1), int(w * bx0):int(w * bx1)]
                if band_scale != 1.0:
                    roi = cv2.resize(roi, None, fx=band_scale, fy=band_scale,
                                     interpolation=cv2.INTER_AREA)
                buf.append((t, roi, frame))
                if len(buf) >= batch_ocr.det_batch:
                    _flush()
            else:
                bx0, by0, bx1, by1 = SCAN_BAND
                roi = frame[int(h * by0):int(h * by1), int(w * bx0):int(w * bx1)]
                if band_scale != 1.0:
                    roi = cv2.resize(roi, None, fx=band_scale, fy=band_scale,
                                     interpolation=cv2.INTER_AREA)
                res, items = _band_items(frame, roi)
                _apply(t, frame, res, items, full_mode=False)
    _flush()
    return [r for r in raw if r[0] >= emit_from]


def _crop(frame, roi):
    h, w = frame.shape[:2]
    return frame[int(h * roi[1]):int(h * roi[3]), int(w * roi[0]):int(w * roi[2])]


def _match_operator(text, op_class):
    """OCR 文本模糊匹配干员名。书法体名卡抖动大（红狼→红伯/红馆），
    先子串后编辑距离 ≤1；仅对≥2 字候选生效，避免单词误中。"""
    t = re.sub(r"[^\u4e00-\u9fff]", "", text)
    if not t:
        return None
    for name in op_class:
        if name in t:
            return name
    for name in op_class:
        if len(name) >= 2 and len(t) >= 2 and _edit_le1(name, t):
            return name
    return None


def _clean_weapon_text(text, wpn_type):
    """枪械读数清洗：剔除弹药计数/射击模式/界面控件/装备部件词，命中枪械表
    时归一化命名。返回标准名或 None。"""
    t = text.strip().replace(" ", "")
    if len(t) < 2 or len(t) > 14:
        return None
    if AMMO_RE.match(t) or CALIBER_RE.search(t):
        return None
    if _LOADOUT_NOISE.search(text) or _NOISE.search(text):
        return None
    if not re.search(r"[A-Za-z\u4e00-\u9fff]", t):   # 纯数字/符号
        return None
    for name in wpn_type:
        if t.upper() == name.upper():
            return name
    # 拉丁枪名编辑距离归一（SCAR H → SCAR-H 型抖动）
    latin = re.match(r"^[A-Za-z0-9\-\.]+$", t)
    if latin:
        for name in wpn_type:
            if re.match(r"^[A-Za-z0-9\-\.]+$", name) and \
                    _edit_le1(t.upper(), name.upper()):
                return name
    # 截断读数归一（"星者"→"坠星者"）：表内唯一 前缀/后缀 匹配才映射
    if len(t) >= 2:
        cands = [n for n in wpn_type if n != t and
                 (n.startswith(t) or n.endswith(t))]
        if len(cands) == 1:
            return cands[0]
    return t          # 未收录：保留原名（类别留空，表可经 config 扩充）


def _step_scan(cap, ocr, t_lo, t_hi, step_sec, rois, accept_box=None):
    """[t_lo, t_hi] 窗口单次 seek + grab 步进采样（逐帧 exact-seek 实测每帧
    1-3s，是 loadout 补扫的性能瓶颈；grab 只解码不转换，1430fps）。对每个
    采样帧按 rois 列表裁剪 OCR，yield (t, roi_idx, box, text, score)。
    accept_box(box, frame_h, frame_w) 为真才产出（几何过滤用）。"""
    cap.set(cv2.CAP_PROP_POS_MSEC, int(t_lo * 1000))
    fps_v = cap.get(cv2.CAP_PROP_FPS) or 25
    n_step = max(1, int(round(fps_v * step_sec)))
    next_t = t_lo
    n = 0
    while True:
        if not cap.grab():
            break
        t = cap.get(cv2.CAP_PROP_POS_MSEC) / 1000.0
        if t > t_hi:
            break
        if n % n_step == 0 and t >= next_t - 0.25:
            ok, frame = cap.retrieve()
            if ok:
                h, w = frame.shape[:2]
                for ri, roi in enumerate(rois):
                    res, _ = ocr(_crop(frame, roi), use_cls=False)
                    for box, text, score in (res or []):
                        if accept_box and not accept_box(box, h, w):
                            continue
                        yield t, ri, box, text, score
            next_t = t + step_sec
        n += 1


def _scan_loadout(path, t_match, op_class, wpn_type):
    """围绕 match_start 时刻补扫本局干员与枪械，返回 meta dict（无命中字段为空）。

    大厅窗 [t-180, t-5]（5s 步进）：干员名卡（书法体，多帧投票）+ 切换播报
    条「出战干员切换为X」（印刷体，一锤定音权重 3，命中即止）。浅色皮肤名卡
    可直读（红狼），深色描金皮肤不可读属已知限制。
    落地窗 [t+30, t+150]（5s 步进）：HUD 右下自己武器名（框高≤0.03H 剔除
    贴片贴纸大字），近战（如坠星者刀）不计主武器；HUD 无稳定读数时回退
    Tab 背包界面（口径/容器行锚定，10s 步进）——贴纸常驻遮挡 HUD 的直播间
    （如猪猪夏）靠该兜底。分片边界处窗口截断属已知限制。
    """
    ocr = _get_ocr()
    cap = cv2.VideoCapture(str(path))
    if not cap.isOpened():
        return {}
    try:
        # -- 干员：大厅窗（播报条 + 名卡投票） --
        # 播报条「出战干员切换为X」是切换瞬间的决定性证据，直接定论；名卡投票
        # 仅在无播报时兜底——名卡可能读到上一局的干员（窗口早段）或误匹配
        # 世界聊天，且切换后播报永远比旧名卡新。
        op_votes, op_best = {}, {}
        toast_name = ""
        for t, ri, box, text, score in _step_scan(
                cap, ocr, max(0.0, t_match - 120.0), t_match - 5.0, 5.0,
                [LOADOUT_ROI_TOAST, LOADOUT_ROI_OP]):
            name = None
            if ri == 0:
                m = OP_SWITCH_RE.search(text)
                name = _match_operator(m.group(1), op_class) if m else None
            else:
                name = _match_operator(text, op_class)
            if name:
                op_votes[name] = op_votes.get(name, 0) + (3 if ri == 0 else 1)
                op_best[name] = max(op_best.get(name, 0.0), float(score))
                if ri == 0:
                    toast_name = name
                    break
        operator = toast_name or (max(op_votes, key=op_votes.get)
                                  if op_votes else "")
        op_conf = op_best.get(operator, 0.0)

        # -- 枪械：HUD 武器名（小字框才算，贴纸大字剔除） --
        def weapon_box(box, h, w):
            ys = [q[1] for q in box]; xs = [q[0] for q in box]
            return (max(ys) - min(ys)) <= h * 0.03 and \
                   (max(xs) - min(xs)) <= w * 0.12

        wpn = {}          # 归一名 -> {reads, first_t, conf}
        def add_wpn(cand, t, score):
            hit = wpn.get(cand) or next(
                (v for k, v in wpn.items()
                 if _edit_le1(k.upper(), cand.upper())
                 or (len(k) >= 2 and len(cand) >= 2 and (k in cand or cand in k))),
                None)
            if hit is None:
                wpn[cand] = {"reads": 1, "first_t": t, "conf": float(score)}
            else:
                hit["reads"] += 1
                hit["conf"] = max(hit["conf"], float(score))

        for t, ri, box, text, score in _step_scan(
                cap, ocr, t_match + 30.0, t_match + 150.0, 5.0,
                [LOADOUT_ROI_WEAPON], weapon_box):
            cand = _clean_weapon_text(text, wpn_type)
            if cand:
                add_wpn(cand, t, score)
                if any(v["reads"] >= 3 for k, v in wpn.items()
                       if wpn_type.get(k) != "近战"):
                    break               # 已有稳定枪械读数（刀不算）

        def guns_stable():
            # 「稳定」= 非近战 ≥2 帧读数；单帧读数视为噪声不做主武器
            return [k for k, v in wpn.items() if v["reads"] >= 2
                    and wpn_type.get(k) != "近战"]

        # -- 枪械兜底：Tab 背包武器列（口径/容器行锚定；逐帧判定整 ROI 文本） --
        tab_used = False
        if not guns_stable():
            tab_used = True
            cap.set(cv2.CAP_PROP_POS_MSEC, int((t_match + 30.0) * 1000))
            fps_v = cap.get(cv2.CAP_PROP_FPS) or 25
            n_step = max(1, int(round(fps_v * 10)))
            n = 0
            while True:
                if not cap.grab():
                    break
                t = cap.get(cv2.CAP_PROP_POS_MSEC) / 1000.0
                if t > t_match + 150.0:
                    break
                if n % n_step == 0:
                    ok, frame = cap.retrieve()
                    if not ok:
                        n += 1
                        continue
                    res, _ = ocr(_crop(frame, LOADOUT_ROI_TAB), use_cls=False)
                    joined = " ".join(x for _, x, _ in (res or []))
                    if not (CALIBER_RE.search(joined)
                            or TAB_MARK_RE.search(joined)):
                        n += 1
                        continue      # 不是背包界面（没开 Tab）
                    for _, text, score in (res or []):
                        cand = _clean_weapon_text(text, wpn_type)
                        # Tab 面板物品名杂（工具/收藏品），只认枪械表内的名字
                        if cand and cand in wpn_type \
                                and wpn_type[cand] != "近战":
                            add_wpn(cand, t, float(score))
                    if any(v["reads"] >= 2 for k, v in wpn.items()
                           if wpn_type.get(k) != "近战"):
                        break
                n += 1

        guns = guns_stable()
        if not guns:
            # 无稳定枪械：纯跑刀（只有近战读数）时以近战兜底，否则留空
            melee = [k for k, v in wpn.items()
                     if v["reads"] >= 2 and wpn_type.get(k) == "近战"]
            guns = melee
        primary = guns[0] if guns else ""
        # 武器列表只保留多帧读数（单帧多为贴纸残片/按键提示噪声）；
        # 全是单帧时保留全部供排查
        stable_names = [k for k, v in wpn.items() if v["reads"] >= 2]
        weapons = [{"name": k, "type": wpn_type.get(k, ""),
                    "reads": wpn[k]["reads"]} for k in
                   sorted(stable_names or wpn,
                          key=lambda k: wpn[k]["first_t"])]
        # 置信度 = 已命中部分（干员 / 主武器）OCR 置信度的最小值
        parts = [op_conf] if operator else []
        if primary:
            parts.append(wpn[primary]["conf"])
        return {
            "operator": operator,
            "operator_class": op_class.get(operator, ""),
            "weapons": weapons,
            "primary_weapon": primary,
            "primary_weapon_type": wpn_type.get(primary, ""),
            "weapon_source": "tab" if tab_used else "hud",
            "operator_reads": dict(op_votes),
            "conf": round(min(parts), 2) if parts else 0.0,
        }
    finally:
        cap.release()


def _scan_loadouts_parallel(path, starts, op_class, wpn_type):
    """loadout 补扫并行：每局一个子进程（模型各自加载，限 2 OCR 线程）。
    串行时每局 15-30s，多局直播会吃掉分段 detect 攒下的时间。失败按 {} 兜底。"""
    import shutil
    import subprocess
    import sys
    import tempfile
    from toolbox.config import ROOT
    tmpdir = tempfile.mkdtemp(prefix="toolbox_loadout_")
    env = dict(os.environ, PYTHONPATH=str(ROOT))
    tables = json.dumps({"operator_class": op_class, "weapon_type": wpn_type})
    procs = []
    try:
        for i, ts in enumerate(starts):
            out_json = os.path.join(tmpdir, f"lo_{ts:.0f}.json")
            cmd = [sys.executable, "-m", "toolbox._loadout_batch_worker",
                   str(path), f"{ts}", tables, out_json]
            # 干员/枪械 ROI 小图 OCR 走 GPU 单帧更快且不占 CPU 核；
            # 奇数号保持 CPU（GPU 显存/上下文有限，一半足够）
            env_w = dict(env, TOOLBOX_OCR_CUDA="1") if i % 2 == 0 else env
            procs.append((subprocess.Popen(cmd, env=env_w), out_json, ts))
        metas = {}
        for p, o, ts in procs:
            ok = p.wait() == 0
            if ok:
                try:
                    with open(o, encoding="utf-8") as f:
                        metas[ts] = json.load(f)
                except Exception:
                    ok = False
            if not ok:
                print(f"  [loadout] 补扫失败 @{ts:.0f}s（worker）", flush=True)
                metas[ts] = {}
        return [metas.get(ts, {}) for ts in starts]
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def _video_duration(path):
    """ffprobe 读视频时长（秒）。"""
    import subprocess
    r = subprocess.run(["ffprobe", "-v", "error", "-show_entries",
                        "format=duration", "-of", "csv=p=0", str(path)],
                       capture_output=True, text=True)
    return float(r.stdout.strip())


def _gpu_workers_available(cfg):
    """config detector.gpu_workers（默认 2）解析：CUDA EP 在装且>0 才生效。
    混合并行下 GPU worker 不占 CPU 核（CPU 聚合吞吐有硬上限，i9-12900H
    实测 ~4.5 帧/s；+2 CUDA worker 后 ~6.6-7 帧/s）。"""
    try:
        n = int((cfg or {}).get("gpu_workers", 2) or 0)
    except (TypeError, ValueError):
        n = 0
    if n <= 0:
        return 0
    try:
        import onnxruntime as ort
        if "CUDAExecutionProvider" in ort.get_available_providers():
            return n
    except Exception:
        pass
    return 0


def _resolve_workers(workers, cfg, path):
    """worker 数解析：显式参数 > config detector.workers > 自动。
    自动 = 短分片(<5min)不值得并行（worker 启动摊不回），长分片按
    每 worker 2 OCR 线程 × min(6, 核数/4)——i9-12900H（20 逻辑核）实测
    OCR 聚合吞吐上限 ~4.5 帧/s，5 worker×2 线程已吃满，更多只互相拖慢。"""
    if workers:
        return workers
    cfg_w = int((cfg or {}).get("workers", 0) or 0)
    if cfg_w > 0:
        return cfg_w
    try:
        dur = _video_duration(path)
    except Exception:
        dur = 1e9
    if dur < 300:
        return 1
    return max(2, min(6, (os.cpu_count() or 4) // 4))


def scan_video(path, fps=1.0, config=None, progress=None, transcript=None,
               workers=None):
    """扫描视频，返回按时间排序的事件列表。

    只 OCR 信息带 SCAN_BAND（目标元素相对位置跨源固定，实测见
    docs/plans/ocr-position-and-crop-study.md）。
    transcript：可选 [{start,end,text}]（ASR 句级转写），用于拾取估价语音交叉。
    workers：None=按 config/时长自动（见 _resolve_workers）；>1 时按时间段分段
    并行（每进程独立 OCR 且限 ORT 线程池，段间重叠 2s 保证边界事件不丢）。
    """
    cfg = (config or {}).get("detector") or {}   # 裸键（子项全注释）YAML 解析为 None
    workers = _resolve_workers(workers, cfg, path)
    price_table = dict(PRICE_TABLE)
    price_table.update(cfg.get("price_table", {}))
    # 信息带降采样系数：默认 1.0（召回最大）。0.8 实测单帧 -35% 但混合并行
    # 下整体不提速（瓶颈是机器聚合吞吐）且回调丢 ~4 条边缘事件，仅留作开关
    try:
        band_scale = float(cfg.get("band_scale", 1.0) or 1.0)
    except (TypeError, ValueError):
        band_scale = 1.0
    pack = (cfg.get("dance_keywords", DANCE_KEYWORDS),
            bool(cfg.get("quality_color_enabled", False)),
            cfg.get("quality_grid", {}),
            price_table,
            band_scale)
    merge_gap = 2.5      # 采样秒，同类同详情事件在此间隔内合并

    if workers and workers > 1:
        # 子进程分段并行：multiprocessing.Pool 在 macOS 上 spawn 死锁、fork 后
        # cv2/AVFoundation 打不开视频（均实测），独立进程是可靠路径。
        # 每段向前多扫 SEG_WARMUP 秒只积累状态不产事件（_scan_range emit_from）：
        # 容器计数基线/待确认拾取/结算跟踪都是跨秒状态，冷启动分段会在边界
        # 丢拾取事件（实测 87 -> 78 条），预热后边界零丢失且不重复
        import os
        import shutil
        import subprocess
        import sys
        import tempfile
        from toolbox.config import ROOT
        dur = _video_duration(path)
        # 混合并行：gpu_workers 个 CUDA worker（不占 CPU 核）+ workers 个 CPU
        # worker；实测 4 CPU + 3 CUDA 聚合 ~6.6 帧/s（纯 CPU 上限 ~4.5）。
        # CPU worker 上限 5：混跑时更多 CPU worker 只会挤占 GPU 的 CPU 侧预处理
        gpu_w = _gpu_workers_available(cfg)
        if gpu_w > 0:
            workers = min(workers, 5)
        n_all = workers + gpu_w
        # 分段按算力加权（GPU_WORKER_WEIGHT）：等分时长时 CPU worker 是长尾、
        # GPU worker 提前完成闲置——混部下实测两头空转差 ~40%
        wts = [GPU_WORKER_WEIGHT] * gpu_w + [1.0] * workers
        w_total = sum(wts)
        pack_json = json.dumps(list(pack))
        tmpdir = tempfile.mkdtemp(prefix="toolbox_scan_")
        env = dict(os.environ, PYTHONPATH=str(ROOT))
        procs, outs = [], []
        t_edge = 0.0
        try:
            for i in range(n_all):
                t_start = t_edge                     # 本段事件产出边界
                t_edge = dur * (sum(wts[:i + 1]) / w_total)
                t_lo = max(0.0, t_start - SEG_WARMUP)   # 预热区间
                t_hi = t_edge
                out_json = os.path.join(tmpdir, f"part{i}.json")
                cmd = [sys.executable, "-m", "toolbox._scan_worker",
                       str(path), f"{t_lo}", f"{t_hi}", f"{fps}",
                       pack_json, out_json, f"{t_start}"]
                env_w = dict(env, TOOLBOX_OCR_CUDA="1", TOOLBOX_OCR_BATCH="1") \
                    if i < gpu_w else env
                procs.append((subprocess.Popen(cmd, env=env_w), out_json))
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

    # 入局装备（loadout）：围绕每个 match_start 补扫干员/枪械。在聚合后、
    # 语音交叉前插入，事件按 t_start 重排时与 match_start 对齐（稳定排序）。
    if cfg.get("loadout_enabled", True):
        op_class = dict(OPERATOR_CLASS)
        op_class.update(cfg.get("operator_class", {}))
        wpn_type = dict(WEAPON_TYPE)
        wpn_type.update(cfg.get("weapon_type", {}))
        match_events = [e for e in events if e.kind == "match_start"]
        starts = [e.t_start for e in match_events]
        if len(starts) > 1 and (workers or 1) > 1:
            metas = _scan_loadouts_parallel(path, starts, op_class, wpn_type)
        else:
            metas = []
            for ts in starts:
                try:
                    metas.append(_scan_loadout(str(path), ts, op_class, wpn_type))
                except Exception as ex:        # 补扫失败不阻断主事件流
                    print(f"  [loadout] 补扫失败 @{ts:.0f}s: {ex}", flush=True)
                    metas.append({})
        for e, meta_l in zip(match_events, metas):
            if not meta_l:
                continue
            op, cls = meta_l["operator"], meta_l["operator_class"]
            parts = []
            if op:
                parts.append(f"干员{op}({cls})" if cls else f"干员{op}")
            if meta_l["primary_weapon"]:
                pw = meta_l["primary_weapon"]
                pt = meta_l["primary_weapon_type"]
                parts.append(f"主武器{pw}({pt})" if pt else f"主武器{pw}")
            elif meta_l["weapons"]:
                parts.append("枪械读数不稳定")
            detail = " ".join(parts) or "入局装备未识别"
            events.append(DetectedEvent(
                "loadout", e.t_start, e.t_start, detail,
                meta_l.pop("conf"), meta_l))
            print(f"  [loadout] @{e.t_start:.0f}s {detail}", flush=True)
        events.sort(key=lambda e: e.t_start)

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
