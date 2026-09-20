"""模板混剪实装：事件流 -> 模板 EDL -> ffmpeg 两步渲染成竖屏短视频。

数据流（对齐 docs/plans/remix-templates-design.md）：
- 片段池：data/reports/*_full/events.jsonl（事件带 src=源视频路径），
  转写 data/asr/*/transcript.json 按 game 字段与源视频匹配，用于句边界 snap。
- plan：模板 yaml 驱动选段/排序/段内压缩 -> EDL json 落盘 data/remixes/<tpl>/。
  素材不限定单一对局——跨局、跨分片合并（叙事弧仍保持 开局->交火->发财->结局）。
  切点再做镜头边界吸附（借鉴 HotClip：句子间隙内找帧差峰值，不切进语音）。
- render：两步法——每段 ffmpeg 精切+blur 竖屏(1080x1920)+loudnorm+叠加元素，
  统一编码参数后 concat demuxer -c copy 拼接。
  成片质量三件套（借鉴 HotClip）：
  - 智能取景：段内显著性锁机位裁切加宽前景（锁机位优先于动态跟随）；
  - 响度归一：成片级两遍 loudnorm 到 -14 LUFS（移动端流媒体标准）；
  - 发布包：cover.jpg 自动封面 + 标题/标签 Jinja2 模板化 -> publish.json。
"""
import json
import re
import subprocess
import tempfile
from pathlib import Path

import yaml

from toolbox.config import DATA, FONTS_DIR, TEMPLATES_DIR, load_config

REQUIRED_KEYS = ("name", "resolution")
FFMPEG = "ffmpeg"
LOUDNESS_I = -14        # 成片目标响度（EBU R128，抖音等移动端标准）


# ---------------------------------------------------------------- 模板

def load_template(name):
    """加载 toolbox/templates/<name>.yaml 并校验必需键。"""
    path = TEMPLATES_DIR / f"{name}.yaml"
    if not path.is_file():
        raise FileNotFoundError(f"模板不存在: {path}")
    tpl = yaml.safe_load(path.read_text(encoding="utf-8"))
    missing = [k for k in REQUIRED_KEYS if k not in tpl]
    if missing:
        raise ValueError(f"模板 {name} 缺少必需键: {missing}")
    return tpl


# ---------------------------------------------------------------- 片段池

def discover_pool():
    """扫描 data/reports/*_full/events.jsonl 建片段池。

    返回 {"videos": {src: {"sents": [...]|None}}, "events": [事件]}，
    事件带 src（源视频绝对路径），转写按 game 字段匹配（无则不 snap）。
    """
    videos, events = {}, []
    for rpt in sorted(DATA.joinpath("reports").glob("*_full/events.jsonl")):
        for line in rpt.read_text(encoding="utf-8").splitlines():
            if line.strip():
                e = json.loads(line)
                e["part_dir"] = rpt.parent.name
                events.append(e)
                if e["src"] not in videos:
                    videos[e["src"]] = {"sents": _match_transcript(e["src"])}
    return {"videos": videos, "events": events}


def _match_transcript(src_video):
    for tpath in sorted(DATA.joinpath("asr").glob("*/transcript.json")):
        t = json.loads(tpath.read_text(encoding="utf-8"))
        if t.get("game") == src_video:
            return t["sentences"]
    return None


def _sents_of(pool, src):
    return pool["videos"].get(src, {}).get("sents")


# ---------------------------------------------------------------- 句边界 snap

def snap_window(t0, t1, sents, cap, min_len=2.5):
    """把 [t0,t1] 窗口外扩到句边界；超 cap 从尾部丢弃整句（不截半句）。

    外扩是加分项而非约束：外扩导致超 cap 时放弃外扩、用原时刻切口
    （ASR 偶发把长篇闲聊并成一句几十秒，外扩到句边界反而会把锚定
    在窗口里的事件截掉——长句内部切口好过丢事件）。
    无转写或时刻落在句间隙时直接用原时刻（间隙内切口不伤语音）。
    """
    if sents:
        s0 = next((s["start"] for s in sents if s["start"] <= t0 < s["end"]), None)
        s1 = next((s["end"] for s in sents if s["start"] <= t1 < s["end"]), None)
        if s0 is not None and (s1 if s1 is not None else t1) - s0 > cap:
            s0 = None                     # 句首外扩会挤掉锚定事件，放弃
        if s1 is not None and s1 - (s0 if s0 is not None else t0) > cap:
            s1 = None                     # 句尾外扩同理
        t0 = s0 if s0 is not None else max(0.0, t0)
        t1 = s1 if s1 is not None else t1
    else:
        t0 = max(0.0, t0)
    if t1 - t0 > cap:                      # 尾部丢整句，直到装得下
        if sents:
            inside = [s for s in sents if t0 <= s["start"] and s["end"] <= t0 + cap]
            if inside:
                t1 = inside[-1]["end"]
        if t1 - t0 > cap:
            t1 = t0 + cap                  # 首句独超 cap，接受硬截
    if t1 - t0 < min_len:
        t1 = t0 + min_len
    return round(t0, 2), round(t1, 2)


# ---------------------------------------------------------------- 镜头边界吸附
# 借鉴 HotClip：切点吸附到附近帧差峰值（开镜/切镜/开关容器界面），
# 且只能在语音句间隙内移动——句结构天然给出可动范围，绝不切进句子。

def _dump_frames(src, t0, dur, fps, size, gray=True):
    """ffmpeg 抽小帧序列（比 cv2 逐点 seek 快且稳），返回 [(t, 图)]。

    gray=True 返回灰度图（帧差分用）；False 返回 RGB（封面等彩色用途）。
    """
    import shutil
    import cv2
    d = tempfile.mkdtemp(prefix="shots_")
    try:
        subprocess.run([FFMPEG, "-hide_banner", "-loglevel", "error", "-y",
                        "-ss", f"{max(0.0, t0):.2f}", "-t", f"{dur:.2f}", "-i", str(src),
                        "-vf", f"fps={fps},scale={size[0]}:{size[1]}",
                        "-pix_fmt", "gray" if gray else "rgb24",
                        str(Path(d) / "f%04d.png")], check=True)
        out = []
        for i, p in enumerate(sorted(Path(d).glob("f*.png"))):
            im = cv2.imread(str(p), cv2.IMREAD_GRAYSCALE if gray else cv2.IMREAD_COLOR)
            if im is None:
                continue
            out.append((t0 + i / fps, cv2.cvtColor(im, cv2.COLOR_BGR2RGB)
                        if not gray else im))
        return out
    finally:
        shutil.rmtree(d, ignore_errors=True)


def _probe_shot_cuts(src, lo, hi):
    """[lo,hi] 内帧间差分 -> 镜头边界候选 [(t, 差分)]。

    差分高于窗口中位数 3 倍（且绝对值 >2）视为边界；窗口太短/探测失败返回 []。
    """
    import numpy as np
    frames = _dump_frames(src, lo, hi - lo, fps=10, size=(160, 90))
    if len(frames) < 3:
        return []
    diffs, times = [], []
    for (t0, a), (t1, b) in zip(frames, frames[1:]):
        diffs.append(float(np.mean(np.abs(a.astype(np.int16) - b.astype(np.int16)))))
        times.append((t0 + t1) / 2)
    med = float(np.median(diffs))
    return [(times[i], d) for i, d in enumerate(diffs) if d > max(med * 3.0, 2.0)]


def snap_to_shot(t, src, sents, shift=1.2):
    """把切点吸附到最近镜头边界；移动不得越过相邻语音句（句间隙内自由移动）。

    t 落在句首（t_in 场景）时该句自身挡住后移、句尾（t_out）时挡住前移，
    间隙内双向可移——语音保护由句结构自动给出，无需方向参数。
    """
    if not src:
        return t
    lo, hi = t - shift, t + shift
    if sents:
        lo = max(lo, max((s["end"] for s in sents if s["end"] <= t + 1e-6), default=0.0))
        hi = min(hi, min((s["start"] for s in sents if s["start"] >= t - 1e-6), default=1e9))
    if hi - lo < 0.1:
        return t
    cuts = _probe_shot_cuts(src, lo, hi)
    near = [(abs(tc - t), tc) for tc, _ in cuts if lo <= tc <= hi]
    return round(min(near)[1], 2) if near else t


def _apply_shot_snap(segs, pool):
    """对每段 t_in/t_out 做镜头边界吸附（EDL 落盘前的最后一道精修）。"""
    for seg in segs:
        if "card" in seg or not seg.get("src"):
            continue
        sents = _sents_of(pool, seg["src"])
        seg["t_in"] = snap_to_shot(seg["t_in"], seg["src"], sents)
        seg["t_out"] = snap_to_shot(seg["t_out"], seg["src"], sents)
    return segs


# ---------------------------------------------------------------- 事件加工

KILL_WORD = {1: "击倒", 2: "双杀", 3: "三杀", 4: "四杀", 5: "五杀"}


def _clusters(events, kind, gap=15.0):
    """同类事件按时间聚簇（同源视频、gap 秒内合并），簇大优先。

    down 默认 15s = 连杀统计口径（按 ASR"杀了三个"交叉校准）。
    """
    evs = sorted((e for e in events if e["kind"] == kind), key=lambda e: e["t_start"])
    out = []
    for e in evs:
        if out and e["src"] == out[-1]["src"] and e["t_start"] - out[-1]["ts"][-1] <= gap:
            out[-1]["ts"].append(e["t_start"])
        else:
            out.append({"ts": [e["t_start"]], "src": e["src"]})
    for c in out:
        c["n"] = len(c["ts"])
    return sorted(out, key=lambda c: -c["n"])


def _match_spans(events):
    """对局分段：[{src, t0, t1, i}]，用于段落字幕标注所属场次。"""
    by_src = {}
    for e in events:
        by_src.setdefault(e["src"], []).append(e)
    spans = []
    for src, evs in by_src.items():
        ms = sorted(e["t_start"] for e in evs if e["kind"] == "match_start")
        for i, b in enumerate(ms):
            spans.append({"src": src, "t0": b,
                          "t1": ms[i + 1] if i + 1 < len(ms) else 1e9, "i": i + 1})
    return spans


def _match_no(spans, src, t):
    return next((sp["i"] for sp in spans if sp["src"] == src and sp["t0"] <= t < sp["t1"]),
                "")


def _chrono(e):
    """跨分片时间序：分片目录名（p1_full<p2_full<...）优先，片内秒数次之。"""
    return (e.get("part_dir", ""), e["t_start"])


def _fmt_wan(price):
    return f"+{round(price / 10000)}万"


def _loot_caption(price):
    if (price or 0) >= 3_000_000:
        return "出大红！"
    if (price or 0) >= 1_000_000:
        return "出红！"
    return "高价值！"


# ---------------------------------------------------------------- 各模板 plan

def plan_fast_cut(pool, tpl):
    """高能快剪：跨类别 top——最强击杀簇做钩子，混估价拾取与成功撤离，先短后长。"""
    segs = []
    for c in _clusters(pool["events"], "down")[:2]:
        sents = _sents_of(pool, c["src"])
        t0, t1 = c["ts"][0] - 6, c["ts"][-1] + 5
        tin, tout = snap_window(t0, t1, sents, cap=min(15.0, max(10.0, t1 - t0)))
        segs.append({"src": c["src"], "t_in": tin, "t_out": tout, "kind": "kill",
                     "count": c["n"], "price": None,
                     "caption": KILL_WORD.get(c["n"], f"{c['n']}杀") + "！"})
    loots = sorted((e for e in pool["events"] if e["kind"] == "loot"
                    and (e.get("price") or e.get("meta", {}).get("voice_hint"))),
                   key=lambda e: -(e.get("price") or 0))[:2]
    for e in loots:
        tin, tout = snap_window(e["t_start"] - 6, e["t_end"] + 8,
                                _sents_of(pool, e["src"]), cap=14)
        segs.append({"src": e["src"], "t_in": tin, "t_out": tout, "kind": "loot",
                     "count": 1, "price": e.get("price"),
                     "caption": _loot_caption(e.get("price"))})
    ok_ext = [e for e in pool["events"] if e["kind"] == "extract" and "成功" in e["detail"]]
    if ok_ext:
        e = ok_ext[-1]
        tin, tout = snap_window(e["t_start"] - 8, e["t_end"] + 6,
                                _sents_of(pool, e["src"]), cap=14)
        segs.append({"src": e["src"], "t_in": tin, "t_out": tout, "kind": "extract",
                     "count": 1, "price": None, "caption": "安全撤离！"})
    segs.sort(key=lambda s: s["t_out"] - s["t_in"])          # 先短后长（完播率）
    hook = max((s for s in segs if s["kind"] == "kill"),
               key=lambda s: s["count"], default=None)
    if hook and segs[0] is not hook:
        segs.remove(hook)
        segs.insert(0, hook)                                  # 最强击杀开头
    return segs[:tpl.get("selection", {}).get("count", 5)]


def plan_hot_kills(pool, tpl):
    """热血击杀：全是击倒簇，烈度交错排序，白闪+跨段累计计数器。

    段内压缩最激进（trim），但多杀簇（>=3）放宽 cap 保完整连杀瞬间。
    人工 pick 优先入选并★标；误报窗内的簇剔除。
    """
    trim = tpl.get("segment", {}).get("trim") or [3, 8]
    count = tpl.get("selection", {}).get("count", 5)
    picks, false_t = _kd_labels(pool)
    clusters = _clusters(_drop_fp(pool["events"], false_t), "down")
    top = _merge_picked(picks, clusters, count)
    order = sorted(top, key=lambda c: -c["n"])
    zig = []                                                  # 大-小-大烈度交错
    while order:
        zig.append(order.pop(0))
        if order:
            zig.append(order.pop())
    segs, total = [], 0
    for c in zig:
        pre, post = tpl.get("pre_roll", 4.0), tpl.get("post_roll", 3.0)
        t0, t1 = c["ts"][0] - pre, c["ts"][-1] + post
        cap = trim[1] if c["n"] < 3 else max(trim[1], min(15.0, t1 - t0))
        tin, tout = snap_window(t0, t1, _sents_of(pool, c["src"]), cap=cap)
        total += c["n"]
        star = "★ " if c.get("picked") else ""
        segs.append({"src": c["src"], "t_in": tin, "t_out": tout, "kind": "kill",
                     "count": c["n"], "price": None,
                     "caption": star + KILL_WORD.get(c["n"], f"{c['n']}杀") + "！",
                     "counter": total})
    return segs


def plan_loot_run(pool, tpl):
    """舔包撤离：估价/语音高价值拾取按价格升序，成功撤离收尾，收益合计卡。

    拾取与撤离可来自不同对局——讲"整晚搬砖"的故事而非单局闭环。
    """
    cap = (tpl.get("segment", {}).get("trim") or [5, 12])[1]
    loots = sorted((e for e in pool["events"] if e["kind"] == "loot"
                    and (e.get("price") or e.get("meta", {}).get("voice_hint"))),
                   key=lambda e: (e.get("price") or 0, e["t_start"]))
    segs, total_price, picked = [], 0, []
    for e in loots:
        if any(e["src"] == p["src"] and abs(e["t_start"] - p["t_start"]) < 30
               for p in picked):     # 同源 30s 内的重复语音提示去重
            continue
        picked.append(e)
        tin, tout = snap_window(e["t_start"] - 6, e["t_end"] + 8,
                                _sents_of(pool, e["src"]), cap=cap)
        price = e.get("price")
        hint = e["meta"].get("voice_hint", [""])[0][:8] if e.get("meta", {}).get("voice_hint") else "高价值"
        tag = (_fmt_wan(price) + " " + hint) if price else hint
        segs.append({"src": e["src"], "t_in": tin, "t_out": tout, "kind": "loot",
                     "count": 1, "price": price,
                     "caption": _loot_caption(price), "price_tag": tag})
        total_price += price or 0
    ok_ext = [e for e in pool["events"] if e["kind"] == "extract" and "成功" in e["detail"]]
    if ok_ext:                                                 # 成功撤离收尾（跨局）
        e = ok_ext[-1]
        tin, tout = snap_window(e["t_start"] - 8, e["t_end"] + 6,
                                _sents_of(pool, e["src"]), cap=16)
        segs.append({"src": e["src"], "t_in": tin, "t_out": tout, "kind": "extract",
                     "count": 1, "price": None, "caption": "撤离成功！",
                     "price_tag": None})
    if total_price:
        segs.append({"card": "profit", "price_total": total_price,
                     "caption": f"本场搬砖合计 ≈ {round(total_price / 10000)}万哈夫币"})
    return segs


def plan_match_story(pool, tpl):
    """整晚复盘：跨局选材——开局部署、多局交火与出货、收官结局。

    素材不限定单一对局（可跨分片合并），段落字幕标注所属场次（"第N场"）；
    叙事弧 开局 -> 交火 -> 发财 -> 结局，收官优先撤离成功，没有则用最后一局结局。
    """
    ev, spans = pool["events"], _match_spans(pool["events"])
    out = []

    def add(src, t0, t1, kind, caption, price=None, count=0, cap=15.0):
        tin, tout = snap_window(t0, t1, _sents_of(pool, src), cap=cap)
        out.append({"src": src, "t_in": tin, "t_out": tout, "kind": kind,
                    "count": count, "price": price, "caption": caption})

    first = min((e for e in ev if e["kind"] == "match_start"), key=_chrono,
                default=None)
    if first:
        add(first["src"], first["t_start"], first["t_start"] + 18,
            "match", "今晚开肝", cap=18)

    for c in _clusters(ev, "down")[:3]:                        # 多局交火（跨局合并）
        no = _match_no(spans, c["src"], c["ts"][0])
        add(c["src"], c["ts"][0] - 6, c["ts"][-1] + 5, "kill",
            f"第{no}场 " + KILL_WORD.get(c["n"], f"{c['n']}杀") + "！",
            count=c["n"], cap=25.0)

    loots = sorted((e for e in ev if e["kind"] == "loot"
                    and (e.get("price") or e.get("meta", {}).get("voice_hint"))),
                   key=lambda e: -(e.get("price") or 0))[:2]   # 发财（跨局）
    for e in loots:
        no = _match_no(spans, e["src"], e["t_start"])
        add(e["src"], e["t_start"] - 6, e["t_end"] + 8, "loot",
            f"第{no}场 " + _loot_caption(e.get("price")), price=e.get("price"))

    exts = [e for e in ev if e["kind"] == "extract"]
    ok = [e for e in exts if "成功" in e["detail"]]
    if ok or exts:
        e = (ok or exts)[-1]                                   # 收官：成功优先
        no = _match_no(spans, e["src"], e["t_start"])
        add(e["src"], e["t_start"] - 8, e["t_end"] + 6, "extract",
            f"第{no}场收官：" + e["detail"][:4], cap=25.0)

    rank = {"match": 0, "extract": 2}                          # 叙事弧排序，弧内按时间
    out.sort(key=lambda s: (rank.get(s["kind"], 1), s["t_in"]))
    return out


def plan_category_mix(pool, tpl):
    """分类合集：单类别（默认 down）按时间线排列，编号字幕。"""
    kind = tpl.get("selection", {}).get("kinds", ["down"])[0]
    if kind != "down":
        raise ValueError(f"分类合集暂只支持 down，得到: {kind}")
    segs = []
    top = _clusters(pool["events"], "down")[:tpl.get("selection", {}).get("count", 6)]
    for c in top:
        no = _match_no(_match_spans(pool["events"]), c["src"], c["ts"][0])
        t0, t1 = c["ts"][0] - 6, c["ts"][-1] + 5
        tin, tout = snap_window(t0, t1, _sents_of(pool, c["src"]), cap=max(12.0, t1 - t0))
        segs.append({"src": c["src"], "t_in": tin, "t_out": tout, "kind": "kill",
                     "count": c["n"], "price": None, "no": no,
                     "caption": KILL_WORD.get(c["n"], f"{c['n']}杀")})
    segs.sort(key=lambda s: (s["src"], s["t_in"]))
    for i, s in enumerate(segs):                     # 时间线排好后再编号
        s["caption"] = f"击杀{i + 1} 第{s.pop('no')}场 " + s["caption"]
    return segs


# ---------------------------------------------------------------- 打标联动
# 击倒线的两类人工标签直接作用于选段（仅这两个模板消费，其余全自动）：
#   picked=true         -> 名场面优先段（score_override 高者先，隐含顺序兜底）
#   verdict=false_positive -> 对应时间窗内的自动击倒簇剔除

def _kd_labels(pool):
    """读 data/knockdowns/<场次>/labels.json -> [(pick_dict), {(src,[误报t])}]。"""
    picks, false_t = [], {}
    for labels_f in sorted(DATA.joinpath("knockdowns").glob("*/labels.json")):
        try:
            labels = json.loads(labels_f.read_text(encoding="utf-8"))
            idx = json.loads((labels_f.parent / "knockdowns.json")
                             .read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        by_file = {c["file"]: c for c in idx.get("clips", [])}
        session = idx.get("session") or ""
        for fname, lab in labels.items():
            c = by_file.get(fname)
            if not c or not c.get("events"):
                continue
            part = str(c.get("part", ""))
            # 事件池可能含多个场次：先按 session 圈定该场次的分片，再按 P 号定位
            src = next((v for v in pool["videos"]
                        if session and session in Path(v).name
                        and re.search(rf"P{re.escape(part)}(?!\d)", Path(v).stem)),
                       None)
            if not src:
                continue
            ts = [e["t"] for e in c["events"]]
            if lab.get("picked") and lab.get("verdict") != "false_positive":
                picks.append({"src": src, "ts": ts, "n": len(ts), "picked": True,
                             "score": lab.get("score_override")})
            if lab.get("verdict") == "false_positive":
                false_t.setdefault(src, []).extend(ts)
    picks.sort(key=lambda p: (p["score"] is not None, p["score"] or 0), reverse=True)
    return picks, false_t


def _drop_fp(events, false_t):
    """剔除被标误报的时间窗（±5s）内的事件。"""
    if not false_t:
        return events
    out = []
    for e in events:
        ts = false_t.get(e["src"], [])
        if any(abs(e["t_start"] - t) <= 5 for t in ts):
            continue
        out.append(e)
    return out


def _merge_picked(picks, clusters, count, radius=45):
    """人工 pick 打头，自动簇去掉与 pick 重叠的，保 count 上限。"""
    out = list(picks)
    for c in clusters:
        if any(c["src"] == p["src"] and abs(c["ts"][0] - p["ts"][0]) < radius
               for p in picks):
            continue
        out.append(c)
    return out[:count]


def plan_single_best(pool, tpl):
    """名场面单段：人工 pick 优先；否则最大击倒簇整段叙事（前滚铺垫，句边界完整）。"""
    picks, false_t = _kd_labels(pool)
    c = picks[0] if picks else _clusters(_drop_fp(pool["events"], false_t), "down")[0]
    t0 = c["ts"][0] - tpl.get("pre_roll", 20.0)
    t1 = c["ts"][-1] + tpl.get("post_roll", 10.0)
    tin, tout = snap_window(t0, t1, _sents_of(pool, c["src"]),
                            cap=tpl.get("segment", {}).get("max_len", 90))
    word = KILL_WORD.get(c["n"], "名场面") + " 完整版"
    if c.get("picked"):
        word = "★ " + word
    return [{"src": c["src"], "t_in": tin, "t_out": tout, "kind": "kill",
             "count": c["n"], "price": None, "caption": word}]


def plan_multi_kill(pool, tpl):
    """多杀变速合集：所有 >=2 连杀簇按时间线排列。

    变速规则：非击杀瞬间 speed_fast 倍速快放；每次击倒前 slow_before 秒
    降为 speed_slow 慢放（相邻慢放区重叠则合并为一个慢区）。
    """
    gap = tpl.get("cluster_gap", 15)
    fast, slow = tpl.get("speed_fast", 1.5), tpl.get("speed_slow", 0.5)
    before = tpl.get("slow_before", 2)
    proll, post = tpl.get("pre_roll", 8), tpl.get("post_roll", 4)
    segs, total = [], 0
    for c in sorted(_clusters(pool["events"], "down", gap=gap),
                    key=lambda c: (c["src"], c["ts"][0])):
        if c["n"] < 2:
            continue
        t0, t1 = c["ts"][0] - proll, c["ts"][-1] + post
        slow_zones = []                       # [t-2, t] 慢放区，重叠合并
        for t in c["ts"]:
            a, b = t - before, t
            if slow_zones and a <= slow_zones[-1][1]:
                slow_zones[-1][1] = b
            else:
                slow_zones.append([a, b])
        zones, cur = [], t0
        for a, b in slow_zones:
            if a > cur:
                zones.append({"t0": round(cur, 2), "t1": round(a, 2), "speed": fast})
            zones.append({"t0": round(max(a, t0), 2), "t1": round(b, 2), "speed": slow})
            cur = b
        if cur < t1:
            zones.append({"t0": round(cur, 2), "t1": round(t1, 2), "speed": fast})
        total += c["n"]
        segs.append({"src": c["src"], "t_in": round(t0, 2), "t_out": round(t1, 2),
                     "kind": "kill", "count": c["n"], "price": None,
                     "zones": zones, "counter": total,
                     "caption": KILL_WORD.get(c["n"], f"{c['n']}杀") + "！"})
    return segs


PLANS = {"fast_cut": plan_fast_cut, "hot_kills": plan_hot_kills,
         "loot_run": plan_loot_run, "match_story": plan_match_story,
         "category_mix": plan_category_mix, "single_best": plan_single_best,
         "multi_kill": plan_multi_kill}


def plan_remix(tpl_name, tpl=None):
    """模板 -> EDL（选段清单 json 落盘），返回 (edl, out_dir)。"""
    tpl = tpl or load_template(tpl_name)
    if tpl_name not in PLANS:
        raise ValueError(f"未知模板策略: {tpl_name}（可用: {sorted(PLANS)}）")
    pool = discover_pool()
    segs = _apply_shot_snap(PLANS[tpl_name](pool, tpl), pool)
    if not segs:
        raise RuntimeError(f"模板 {tpl_name} 选不出任何片段（事件池为空或约束过强）")
    out_dir = DATA / "remixes" / tpl_name
    out_dir.mkdir(parents=True, exist_ok=True)
    n_down = sum(1 for e in pool["events"] if e["kind"] == "down")
    n_ok = sum(1 for e in pool["events"]
               if e["kind"] == "extract" and "成功" in e["detail"])
    edl = {"template": tpl_name, "resolution": tpl["resolution"],
           "title_card": tpl.get("title_card"),
           "pool_stats": {"n_down": n_down, "n_extract_ok": n_ok,
                          "n_events": len(pool["events"])},
           "segments": segs}
    (out_dir / "edl.json").write_text(json.dumps(edl, ensure_ascii=False, indent=1),
                                      encoding="utf-8")
    return edl, out_dir


# ---------------------------------------------------------------- 渲染
# 本机 ffmpeg 无 drawtext/subtitles 滤镜，中文文字一律 PIL 渲染为 PNG 后
# 用 overlay 滤镜叠加（排版可控，且能按宽度自动缩号防溢出）。

def _pick_font(config=None):
    """中文字体解析（应用端 macOS/Windows / 服务端 Linux 通用）：
    remix.font_path 配置 > 平台系统字体 > 包内 fonts/。找不到时明确报错，
    避免 PIL 在服务器上静默用错字体。注意：本函数在模块导入时执行
    （FONT_PATH），任何经 report->remix 的导入链都会触发，缺平台候选会让
    与渲染无关的命令（如 pickups）在导入期就报错。"""
    import sys
    configured = ((config or {}).get("remix") or {}).get("font_path") or ""
    cands = []
    if configured:
        cands.append(Path(configured).expanduser())
    if sys.platform == "darwin":
        cands += [Path("/System/Library/Fonts/STHeiti Light.ttc"),
                  Path("/System/Library/Fonts/PingFang.ttc"),
                  Path("/Library/Fonts/Arial Unicode.ttf")]
    elif sys.platform == "win32":
        cands += [Path("C:/Windows/Fonts/msyh.ttc"),          # 微软雅黑
                  Path("C:/Windows/Fonts/msyhbd.ttc"),
                  Path("C:/Windows/Fonts/simhei.ttf")]
    else:
        cands += [FONTS_DIR / "NotoSansSC-Bold.otf",
                  Path("/usr/share/fonts/google-noto-sans-cjk-fonts/NotoSansCJK-Bold.ttc"),
                  Path("/usr/share/fonts/google-noto-cjk/NotoSansCJK-Bold.ttc"),
                  Path("/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc")]
    for c in cands:
        if c and c.is_file():
            return str(c)
    raise RuntimeError(
        "未找到可用中文字体：在 config.yaml 配置 remix.font_path，"
        "或安装 Noto Sans CJK（服务器镜像已含），或放一个字体到 toolbox/fonts/")


FONT_PATH = _pick_font(load_config())


def _card_png(text, w, h):
    """整帧黑底文字卡 PNG（标题卡/收益合计卡），字号按宽度自适应。"""
    from PIL import Image, ImageDraw, ImageFont
    size = 72
    font = ImageFont.truetype(FONT_PATH, size)
    probe = ImageDraw.Draw(Image.new("RGBA", (8, 8)))
    tw = probe.textbbox((0, 0), text, font=font)[2]
    if tw > w - 80:                                          # 超宽自动缩号
        size = max(28, int(size * (w - 80) / tw))
        font = ImageFont.truetype(FONT_PATH, size)
    img = Image.new("RGB", (w, h), "black")
    d = ImageDraw.Draw(img)
    box = d.textbbox((0, 0), text, font=font)
    d.text(((w - box[2] + box[0]) / 2, h * 0.44), text, font=font,
           fill=(255, 255, 255))
    fd, path = tempfile.mkstemp(suffix=".png")
    img.save(path)
    import os
    os.close(fd)
    return path


def _overlays_png(items, w, h):
    """多行叠加文字合成到一张全画布透明 PNG（单次 overlay，无多路同步问题）。

    items: [(text, y_top, size, rgb)]，文字水平居中、黑描边。
    """
    from PIL import Image, ImageDraw, ImageFont
    img = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    for text, y, size, color in items:
        font = ImageFont.truetype(FONT_PATH, size)
        box = d.textbbox((0, 0), text, font=font, stroke_width=6)
        d.text(((w - box[2] + box[0]) / 2, y), text, font=font,
               fill=tuple(color) + (255,), stroke_width=6,
               stroke_fill=(0, 0, 0, 255))
    fd, path = tempfile.mkstemp(suffix=".png")
    img.save(path)
    import os
    os.close(fd)
    return path


def _blur_pad_vf(res, crop=None):
    """横屏源 -> WxH 竖屏：模糊放大垫底 + 前景居中，输出标签 [base]。

    crop=(w,h,x) 时前景先裁切（智能取景：前景加宽、主体放大）再缩放。
    背景先缩到 1/4 尺寸再 gblur 再放大——大 sigma 直接糊全分辨率极慢，
    缩小后模糊视觉等价，快一个数量级。
    """
    w, h = (int(x) for x in res.lower().split("x"))
    qw, qh = w // 4, h // 4
    fgv = (f"crop={crop[0]}:{crop[1]}:{crop[2]}:0[c];[c]" if crop else "")
    return (f"[0:v]split=2[bg][fg];"
            f"[bg]scale={qw}:{qh}:force_original_aspect_ratio=increase,"
            f"crop={qw}:{qh},gblur=sigma=6,scale={w}:{h}[bgb];"
            f"[fg]{fgv}scale={w}:-2[fgs];"
            f"[bgb][fgs]overlay=(W-w)/2:(H-h)/2[base]")


# ---------------------------------------------------------------- 智能取景
# 借鉴 HotClip「锁机位优先于跟随」：段内采样帧差分的列质心稳定则锁在质心处，
# 不稳定回退居中锁。不做动态跟随（需 sendcmd 逐帧驱动 crop，复杂度不匹配
# 收益；FPS 视角动作围绕准星，静态裁切已覆盖）。

def _lock_crop_x(src, t0, t1, src_w, crop_w, max_frames=16):
    """段内显著性列质心 -> 锁定 x；质心漂移超宽（0.12*源宽）回退居中。"""
    import numpy as np
    dur = max(0.6, t1 - t0)
    frames = _dump_frames(src, t0, dur, fps=min(2.0, max_frames / dur),
                          size=(192, 108))
    prev, cents = None, []
    for _, im in frames:
        if prev is not None:
            cols = np.abs(im.astype(np.int16) - prev.astype(np.int16)).sum(axis=0)
            if cols.sum() > 1e-3:
                idx = np.arange(len(cols))
                cents.append(float((cols * idx).sum() / cols.sum()) / len(cols))
        prev = im
    center = (src_w - crop_w) // 2
    if not cents:
        return center
    mean = sum(cents) / len(cents)
    if max(abs(c - mean) for c in cents) > 0.12:    # 显著性不稳定 -> 居中锁
        return center
    return max(0, min(src_w - crop_w, round(mean * src_w - crop_w / 2)))


def plan_reframe(seg, res, tpl):
    """为一段素材决定裁切窗口 (crop_w, crop_h, x)；返回 None 保持原 letterbox。

    模板 reframe.crop_ratio（默认 0.75：1080p 源裁 1440 宽，前景加宽 33%）。
    放弃取景的三种情况：模板关闭 / 裁窗不小于源宽 / 裁窗 <540px
    （上采样超 2 倍，480p 源画质不可接受）。
    """
    cfg = tpl.get("reframe") or {}
    if not cfg.get("enabled", True) or not seg.get("src"):
        return None
    w, h = (int(x) for x in res.lower().split("x"))
    out = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0",
                          "-show_entries", "stream=width,height", "-of", "csv=p=0",
                          str(seg["src"])], capture_output=True, text=True)
    try:
        sw, sh = (int(x) for x in out.stdout.strip().split(","))
    except ValueError:
        return None
    crop_w = max(round(sw * cfg.get("crop_ratio", 0.75)) // 2 * 2,
                 round(sh * w / h) // 2 * 2)         # 前景不得高过竖屏画幅
    if crop_w >= sw or crop_w < 540:
        return None
    return (crop_w, sh, _lock_crop_x(seg["src"], seg["t_in"], seg["t_out"], sw, crop_w))


_ENC = ("-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
        "-pix_fmt", "yuv420p", "-r", "30",
        "-c:a", "aac", "-b:a", "160k", "-ar", "44100", "-ac", "2")


def _render_clip(seg, out, a, b, speed, res, tpl, flash, crop=None):
    """渲染 [a,b] 源区间为一条片段：speed!=1 时 setpts 变速 + atempo 保音调。"""
    w, h = (int(x) for x in res.lower().split("x"))
    items = []                                    # [(text, y, size, rgb)] 自上而下
    if seg.get("price_tag"):
        items.append((seg["price_tag"], 260, 58, (255, 215, 0)))
    if seg.get("counter") and tpl.get("overlays", {}).get("kill_counter"):
        items.append((f"×{seg['counter']} 击倒", 380, 80, (255, 80, 80)))
    if tpl.get("caption", True) and seg.get("caption"):
        items.append((seg["caption"], 1480, 64, (255, 255, 255)))

    tail = (",fade=t=in:st=0:d=0.12:color=white"         # 白闪转场（E 模板）
            if flash and tpl.get("transition") == "cut+flash" else ",null")
    if speed != 1.0:                     # 变速：成片时长 = 源时长/速度
        tail += f",setpts=PTS/{speed}"
    if items:      # 所有文字合成一张全画布透明 PNG，单次 overlay 叠加
        graph = (_blur_pad_vf(res, crop)
                 + ";[base][1:v]overlay=0:0" + tail + "[v]")
        inputs = ["-i", _overlays_png(items, w, h)]
    else:
        graph = _blur_pad_vf(res, crop) + ";[base]" + tail[1:] + "[v]"
        inputs = []

    dur = b - a
    af = f"loudnorm=I={LOUDNESS_I}:TP=-1.5:LRA=11"
    if speed != 1.0:
        af = f"atempo={speed},{af}"
    subprocess.run([FFMPEG, "-hide_banner", "-loglevel", "error", "-y",
                    "-ss", f"{a:.2f}", "-t", f"{dur:.2f}",
                    "-i", seg["src"], *inputs,
                    "-filter_complex", graph,
                    "-map", "[v]", "-map", "0:a?",
                    "-af", af,
                    *_ENC, str(out)], check=True)
    return out.name


def render_segment(seg, idx, res, tpl, work):
    """渲染单段为统一参数的 seg_NN*.mp4 列表（变速段按 zones 展开为多条）。"""
    if "card" in seg:                                          # 黑底文字卡
        out = work / f"seg_{idx:02d}.mp4"
        w, h = (int(x) for x in res.lower().split("x"))
        dur = 1.6 if seg["card"] == "profit" else 0.8
        png = _card_png(seg["caption"], w, h)
        subprocess.run([FFMPEG, "-hide_banner", "-loglevel", "error", "-y",
                        "-loop", "1", "-t", f"{dur}", "-i", png,
                        "-f", "lavfi", "-i", "anullsrc=r=44100:cl=stereo",
                        "-t", f"{dur}", "-map", "0:v", "-map", "1:a",
                        *_ENC, str(out)], check=True)
        return [out.name]
    crop = plan_reframe(seg, res, tpl)          # 智能取景：每段锁一次机位
    seg["_crop"] = crop
    if seg.get("zones"):                        # 变速段：逐区切条，首区白闪
        return [_render_clip(seg, work / f"seg_{idx:02d}_{k}.mp4",
                             z["t0"], z["t1"], z["speed"], res, tpl,
                             flash=(k == 0), crop=crop)
                for k, z in enumerate(seg["zones"])]
    out = work / f"seg_{idx:02d}.mp4"
    return [_render_clip(seg, out, seg["t_in"], seg["t_out"], 1.0, res,
                         tpl, flash=True, crop=crop)]


def normalize_loudness(final):
    """成片级两遍 loudnorm 到 LOUDNESS_I：先整体测量，再线性修正（视频流直拷）。

    返回归一前的整体响度（LUFS）；无声流或测量失败返回 None（保持原样）。
    段内单遍 loudnorm 只保证相对一致，绝对目标必须整片两遍测量才准。
    """
    import re
    probe = subprocess.run(
        [FFMPEG, "-hide_banner", "-nostats", "-i", str(final),
         "-af", f"loudnorm=I={LOUDNESS_I}:TP=-1.5:LRA=11:print_format=json",
         "-f", "null", "-"], capture_output=True, text=True)
    m = re.search(r"\{\s*\"input_i\"[^}]*\}", probe.stderr)
    if not m:
        return None
    st = json.loads(m.group(0))
    af = (f"loudnorm=I={LOUDNESS_I}:TP=-1.5:LRA=11:linear=true"
          f":measured_I={st['input_i']}:measured_TP={st['input_tp']}"
          f":measured_LRA={st['input_lra']}:measured_thresh={st['input_thresh']}"
          f":offset={st['target_offset']}")
    tmp = final.with_name(final.stem + ".norm.mp4")
    subprocess.run([FFMPEG, "-hide_banner", "-loglevel", "error", "-y",
                    "-i", str(final), "-af", af,
                    "-c:v", "copy", "-c:a", "aac", "-b:a", "160k",
                    "-movflags", "+faststart", str(tmp)], check=True)
    tmp.replace(final)
    return float(st["input_i"])


def render_remix(edl, out_dir, tpl):
    """按 EDL 两步渲染：逐段统一参数编码 -> concat copy 拼接 + 解码校验。"""
    work = out_dir / "seg"
    work.mkdir(exist_ok=True)
    res, parts = edl["resolution"], []
    tc = edl.get("title_card") or {}
    if tc.get("text"):
        parts += render_segment({"card": "title", "caption": tc["text"]},
                                98, res, tpl, work)
    for i, seg in enumerate(edl["segments"]):
        parts += render_segment(seg, i, res, tpl, work)
        zones = seg.get("zones")
        crop = seg.get("_crop")
        frame = (f"锁机位x={crop[2]}" if crop else "整幅") + \
            (f" 裁{crop[0]}px" if crop else "")
        print(f"  [{Path(seg.get('src', 'CARD')).name[:26]:<26}] "
              f"{seg.get('t_in', 0):>7.1f}-{seg.get('t_out', 0):>7.1f}"
              f"{f'  {len(zones)}个变速区' if zones else ''}"
              f"  {frame}"
              f"  {seg.get('caption', '')[:22]}", flush=True)
    concat = out_dir / "concat.txt"
    concat.write_text("".join(f"file '{work / p}'\n" for p in parts),
                      encoding="utf-8")
    final = out_dir / f"{edl['template']}.mp4"
    subprocess.run([FFMPEG, "-hide_banner", "-loglevel", "error", "-f", "concat",
                    "-safe", "0", "-i", str(concat), "-c", "copy",
                    "-movflags", "+faststart", "-y", str(final)], check=True)
    subprocess.run([FFMPEG, "-hide_banner", "-v", "error", "-i", str(final),
                    "-f", "null", "-"], check=True)            # 解码全通校验
    return final


# ---------------------------------------------------------------- 发布包
# 借鉴 HotClip 的"平台发布包"：不自动上传，产出封面 + 标题/标签文案
# （Jinja2 模板渲染，模板 yaml 的 publish 段可覆盖默认值）。

DEFAULT_PUBLISH = {
    "title": "{{ streamer }}三角洲行动高光 | {{ hook }}",
    "tags": "三角洲行动,{{ streamer }},高光时刻,{{ template_name }}",
    "cover_text": "{{ streamer }} · {{ hook }}",
}


def _publish_vars(edl, tpl):
    segs = [s for s in edl["segments"] if "card" not in s]
    src0 = next((s.get("src") for s in segs if s.get("src")), "")
    stats = edl.get("pool_stats") or {}
    return {
        "streamer": Path(src0).name.split("（")[0] or "主播",
        "hook": next((s["caption"] for s in segs if s.get("caption")), ""),
        "n_kills": sum(s.get("count", 0) for s in segs if s.get("kind") == "kill"),
        "n_segments": len(segs),
        "n_down_total": stats.get("n_down", 0),
        "n_extract_ok": stats.get("n_extract_ok", 0),
        "profit_wan": round(sum(s.get("price") or 0 for s in edl["segments"]) / 10000),
        "template_name": tpl.get("name", edl["template"]),
    }


def _make_cover(edl, out_dir, cover_text, badge):
    """钩子段源视频抽帧 + PIL 合成 1080x1920 封面，返回 cover.jpg 路径。

    构图沿用该段的锁机位裁窗（_crop）——封面与成片取景一致。
    """
    from PIL import Image, ImageDraw, ImageFont
    seg = next((s for s in edl["segments"]
                if s.get("kind") == "kill" and s.get("src")), None) \
        or next((s for s in edl["segments"] if s.get("src")), None)
    if not seg:
        return None
    t = seg["t_in"] + min(1.5, (seg["t_out"] - seg["t_in"]) / 2)
    frames = _dump_frames(seg["src"], t, 0.6, fps=1, size=(1920, 1080),
                          gray=False)          # 输入 -t 过短会被量化成 0 帧
    if not frames:
        return None
    frame = Image.fromarray(frames[0][1]).convert("RGB")
    W, H = 1080, 1920
    scale = H / frame.height
    big = frame.resize((round(frame.width * scale), H))
    crop = seg.get("_crop")
    if crop:
        x = max(0, min(big.width - W, round(crop[2] * scale)))
    else:
        x = (big.width - W) // 2
    canvas = big.crop((x, 0, x + W, H))

    grad = Image.new("L", (1, H))
    for y in range(H):                       # 下半渐暗，衬托封面字
        grad.putpixel((0, y), max(0, min(230, int((y - H * 0.45) / (H * 0.55) * 230))))
    canvas = Image.composite(Image.new("RGB", (W, H), "black"), canvas,
                             grad.resize((W, H)))
    d = ImageDraw.Draw(canvas)

    def fit(text, size):
        font = ImageFont.truetype(FONT_PATH, size)
        box = d.textbbox((0, 0), text, font=font, stroke_width=8)
        if box[2] - box[0] > W - 120:       # 超宽自动缩号
            size = max(36, int(size * (W - 120) / (box[2] - box[0])))
            font = ImageFont.truetype(FONT_PATH, size)
            box = d.textbbox((0, 0), text, font=font, stroke_width=8)
        return font, box

    badge_y, main_y = int(H * 0.66), int(H * 0.73)
    fitted = [(t, y, c, *fit(t, s))
              for t, y, s, c in ((badge, badge_y, 64, (255, 215, 0)),
                                 (cover_text, main_y, 110, (255, 255, 255)))]
    # 半透明黑圆角底衬：金徽标在黄色游戏场景里曾近乎隐形，任意底图下保可读
    pad, top = 44, 26
    bw = max(b[2] - b[0] for *_, b in fitted) + pad * 2
    bh = fitted[-1][1] + fitted[-1][4][3] - fitted[0][1] + top * 2
    ov = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    ImageDraw.Draw(ov).rounded_rectangle(
        ((W - bw) / 2, fitted[0][1] - top, (W + bw) / 2,
         fitted[0][1] - top + bh), radius=30, fill=(0, 0, 0, 150))
    canvas = Image.alpha_composite(canvas.convert("RGBA"), ov).convert("RGB")
    d = ImageDraw.Draw(canvas)
    for t, y, color, font, box in fitted:
        d.text(((W - box[2] + box[0]) / 2, y), t, font=font,
               fill=color, stroke_width=8, stroke_fill=(0, 0, 0))
    cover = out_dir / "cover.jpg"
    canvas.save(cover, quality=88)
    return cover


def publish_pack(edl, out_dir, tpl, final, measured_i=None):
    """生成发布包：cover.jpg + publish.json（标题/标签 Jinja2 模板渲染）。"""
    from jinja2 import Environment
    vars = _publish_vars(edl, tpl)
    pub = {**DEFAULT_PUBLISH, **(tpl.get("publish") or {})}
    env = Environment()

    def render(s):
        return env.from_string(s).render(**vars).strip()
    title = render(pub["title"])
    tags = [t.strip() for t in render(pub["tags"]).split(",") if t.strip()]
    badge = vars["hook"] or (f"一晚{vars['n_down_total']}杀"
                             if vars["n_down_total"] else vars["template_name"])
    cover = _make_cover(edl, out_dir, render(pub["cover_text"]), badge)
    info = subprocess.run(["ffprobe", "-v", "error", "-show_entries",
                           "format=duration", "-of", "csv=p=0", str(final)],
                          capture_output=True, text=True).stdout.strip()
    pack = {"video": str(final), "title": title, "tags": tags,
            "cover": str(cover) if cover else None,
            "duration_s": round(float(info or 0), 1),
            "loudness_lufs": LOUDNESS_I if measured_i is not None else None,
            "measured_before_lufs": measured_i,
            "template": edl["template"], "vars": vars}
    (out_dir / "publish.json").write_text(
        json.dumps(pack, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"发布包: {out_dir}/（{final.name} + "
          f"{'cover.jpg + ' if cover else ''}publish.json）")
    print(f"  标题: {title}")
    print(f"  标签: {' '.join('#' + t for t in tags)}")
    return pack


def remix(template_name):
    """模板混剪入口：plan（含镜头吸附）-> render（含锁机位）-> 响度归一 -> 发布包。"""
    tpl = load_template(template_name)
    edl, out_dir = plan_remix(template_name, tpl)
    print(f"模板 {tpl['name']}（{template_name}）：{len(edl['segments'])} 段")
    final = render_remix(edl, out_dir, tpl)
    measured = normalize_loudness(final)
    total = sum((z["t1"] - z["t0"]) / z["speed"] if s.get("zones")
                else s.get("t_out", 0) - s.get("t_in", 0)
                for s in edl["segments"]
                for z in (s.get("zones") or [{"t0": 0, "t1": 0, "speed": 1}]))
    if measured is not None:
        print(f"响度: {measured:.1f} -> {LOUDNESS_I} LUFS（两遍 loudnorm）")
    print(f"成片: {final}（成片时长约 {total:.0f}s）")
    publish_pack(edl, out_dir, tpl, final, measured_i=measured)
    return final
