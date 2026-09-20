"""主播语音情绪评估：从整场直播里选「情绪高昂、激烈」的解说片段（纯音频）。

产物是 m4a 音频片段，边界对齐 ASR 句子（一段完整的话，不截半句）。
设计见 docs/plans/voice-emotion-plan.md，v2 要点：

- 场次内自适应校准：音量/基频受设备增益与音色影响，跨场次固定阈值
  不可比。所有维度先算原始信号，再映射成「场次内分位」——信号分布
  取自 L1 滑窗（8s 窗 2s 步）与 yin 抽样窗，每个视频自成标准；
- L1 全场粗扫（numpy，秒级）：RMS 包络 + host 句字符流（语速/情绪词/
  叠字）→ 窗口信号序列 + hype（分位混合）曲线 → p85 阈值取候选段，
  边界吸附到 host 句（12-120s）；
- L2 片段精算（librosa.yin 只对候选段 + 抽样窗跑）：升调/喊声占比。

维度：声强V（音量峰/爆发）×音调T（升调/喊声）×语流F（语速/连讲）
×语义W（情绪词/叠字），值均为场次内分位 ∈[0,1]；
总分=0.30V+0.25T+0.20F+0.25W，S/A/B/C 同击打分阈值。

输入：data/asr/<场次>/（transcript.json；speaker_labeled.json 存在则只认
host 句；audio_16k.wav 是唯一必需音源——源视频已删的场次也能跑）。
产出：data/voice/<场次>/NN_MMmSSs_首句摘要.m4a + voice_scores.json；
--html 汇总全部场次生成 data/voice/review.html（风格同击倒 review）。
"""
import json
import random
import re
import subprocess
import wave
from pathlib import Path

import numpy as np

from toolbox.config import DATA, load_config
from toolbox.knockdown import _mmss, _slug

SR = 16000

# 情绪词表：多字词直接子串计数；单字词句内出现>=2 次才计（避免"牛奶"误伤）
EMOTION_WORDS = (
    "卧槽 我槽 我去 我草 我靠 我嘞个 好家伙 天呐 天哪 我的天 妈呀 妈耶 "
    "牛逼 牛批 牛皮 绝了 离谱 离大谱 炸了 起飞 完蛋 寄了 上头 笑死 哈人 "
    "吓死 救命 芜湖 冲啊 完了完了 漂亮 打得漂亮 666 牛 帅 爽 杀 冲 寄"
).split()
_SINGLE = {w for w in EMOTION_WORDS if len(w) == 1}
# 重复感叹：叠字白名单（"啊啊啊/牛牛牛/哈哈哈哈"），>=4 字记 2 次
_REPEAT_CHARS = set("啊呀嘛哈牛杀冲寄走完我去草槽救妈天6哦嘿哇耶")

HOP = 0.25              # RMS 粒度（秒）
WIN, STEP = 8.0, 2.0    # L1 滑窗/步进
HYPER_PCTL = 85         # hype 阈值分位：高于即候选
MERGE_GAP = 10.0        # 候选窗合并间隙
MIN_SEG, MAX_SEG = 12.0, 120.0    # 片段时长界（句对齐后）
GAP_SPEECH = 1.2        # 句间停顿超过此值算讲述中断
TAIL_PAD = 0.5          # 末句后尾音留白
F0_SAMPLES = 50         # yin 抽样窗数（场次音高分位参考分布）

W_GROUP = {"声强V": 0.30, "音调T": 0.25, "语流F": 0.20, "语义W": 0.25}
# 维度值是候选段集合内的相对分位，中位天然 0.5，阈值按此尺度定
GRADE = [(78, "S"), (64, "A"), (48, "B"), (0, "C")]


def _grade(s):
    for th, g in GRADE:
        if s >= th:
            return g
    return "C"


def _rank01(ref, v):
    """v 在参考分布 ref 中的分位（0-1）。参考集空/NaN 走中性 0.5。"""
    if v is None or not len(ref):
        return 0.5
    return float((np.asarray(ref) < v).mean())


# ---------------------------------------------------------------- 输入解析

def resolve_asr(target):
    """asr 目录或 transcript.json 路径 -> (meta, sentences, audio_path)。

    只需 audio_16k.wav（缺则从 transcript.game 源视频提）；
    speaker_labeled.json 存在则只保留 host 句（TTS/唱歌不污染语义）。
    """
    p = Path(target).expanduser().resolve()
    tpath = p / "transcript.json" if p.is_dir() else p
    if not tpath.exists():
        raise FileNotFoundError(f"找不到 transcript.json: {tpath}")
    meta = json.loads(tpath.read_text(encoding="utf-8"))

    labeled = tpath.parent / "speaker_labeled.json"
    if labeled.exists():
        d = json.loads(labeled.read_text(encoding="utf-8"))
        sents = [s for s in d["sentences"] if s.get("speaker") == "host"]
        print(f"  用 speaker_labeled：host 句 {len(sents)}"
              f"/{len(d['sentences'])}（排除 TTS/唱歌）")
    else:
        sents = meta["sentences"] if isinstance(meta, dict) else meta
        print("  [提示] 无 speaker_labeled.json，语义维度含播报/唱歌句")
    if not sents:
        raise RuntimeError("没有可用的 host 句")

    audio = tpath.parent / "audio_16k.wav"
    if not audio.exists():
        game = meta.get("game")
        if not game or not Path(game).exists():
            raise FileNotFoundError(
                f"音频不存在且无源视频可提: {audio}")
        print("  提取 audio_16k.wav ...")
        subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error",
                        "-i", game, "-vn", "-ac", "1", "-ar", str(SR),
                        "-acodec", "pcm_s16le", "-y", str(audio)], check=True)
    return meta, sents, audio


# ---------------------------------------------------------------- L1 窗口信号

def rms_timeline(wav_path, hop=HOP):
    """整场 RMS 包络，分块读防长直播内存爆。返回 (times, rms)。"""
    n = int(SR * hop)
    chunks = []
    with wave.open(str(wav_path)) as w:
        while True:
            raw = w.readframes(SR * 120)      # 120s 一块
            if not raw:
                break
            d = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
            d = d[:len(d) // n * n].reshape(-1, n)
            chunks.append(np.sqrt((d ** 2).mean(axis=1)))
    rms = np.concatenate(chunks)
    return np.arange(len(rms)) * hop, rms


def char_bins(sentences, dur, bin_s=0.5):
    """host 句铺到 0.5s bin：(chars, emo, rep) 逐 bin 计数。

    长句（VAD 大句把停顿并进来）字符线性铺在 [start,end] 上，窗口聚合
    时近似成立；情绪词/叠字按句计，落在句中点 bin。
    """
    nb = int(dur / bin_s) + 1
    chars = np.zeros(nb)
    emo = np.zeros(nb)
    rep = np.zeros(nb)
    for s in sentences:
        text = s.get("text") or ""
        c = len(text.replace(" ", ""))
        if not c:
            continue
        i0, i1 = int(s["start"] / bin_s), int(np.ceil(s["end"] / bin_s))
        i0, i1 = max(0, i0), min(nb, max(i0 + 1, i1))
        chars[i0:i1] += c / (i1 - i0)
        mid = min(nb - 1, int((s["start"] + s["end"]) / 2 / bin_s))
        emo[mid] += _count_emotion(text)
        rep[mid] += _count_repeat(text)
    return chars, emo, rep


def _count_emotion(text):
    n = 0
    for w in EMOTION_WORDS:
        if w in _SINGLE:
            if text.count(w) >= 2:
                n += text.count(w) - 1
        else:
            n += text.count(w)
    return n


def _count_repeat(text):
    n = 0
    for m in re.finditer(r"(.)\1+", text):
        if m.group(1) in _REPEAT_CHARS:
            n += 2 if len(m.group()) >= 4 else 1
    return n


def host_mask_bins(sentences, dur):
    """host 句覆盖掩码（0.25s 粒度）：能量/基频统计只看这些窗。"""
    m = np.zeros(int(dur / HOP) + 1, dtype=bool)
    for s in sentences:
        m[int(s["start"] / HOP):int(s["end"] / HOP) + 1] = True
    return m


def window_signals(rms, chars, emo, rep, sentences, dur):
    """全场滑窗原始信号 + 分位参考分布。返回 (win_rows, refs, base)。

    win_rows: [{t, vol, burst, spd, emo_pm, rep_pm, host_frac}]，
    refs: 各信号的场次内参考分布（host_frac>0.5 的窗），
    base: {rms_p50, rate_med} 场次基线。
    """
    bin_s = 0.5
    host_mask = host_mask_bins(sentences, dur)
    n_r = min(len(rms), len(host_mask))
    rms, host_mask = rms[:n_r], host_mask[:n_r]
    r_host = rms[host_mask]
    p50 = float(np.median(r_host)) if len(r_host) else 1e-6
    rates_all = [len((s.get("text") or "").replace(" ", "")) /
                 max(0.5, s["end"] - s["start"])
                 for s in sentences if s["end"] - s["start"] >= 1.0]
    rate_med = float(np.median(rates_all)) if rates_all else 3.0

    w_bins, s_bins = int(WIN / bin_s), int(STEP / bin_s)
    rows = []
    for i in range(0, len(chars) - w_bins + 1, s_bins):
        j0, j1 = int(i * bin_s / HOP), int((i + w_bins) * bin_s / HOP)
        m = host_mask[j0:j1]
        frac = float(m.mean())
        t = (i + w_bins / 2) * bin_s
        row = {"t": t, "vol": 0.0, "burst": 0.0, "spd": 0.0,
               "emo_pm": 0.0, "rep_pm": 0.0, "host_frac": frac}
        w_chars = chars[i:i + w_bins].sum()
        if w_chars > 0:
            row["spd"] = float(w_chars / WIN) / rate_med
            row["emo_pm"] = float(emo[i:i + w_bins].sum()) * 60 / WIN
            row["rep_pm"] = float(rep[i:i + w_bins].sum()) * 60 / WIN
        if frac > 0.5 and j1 > j0:
            r = rms[j0:j1]
            rw = r[m[:len(r)]]
            med = float(np.median(rw)) if len(rw) else 0.0
            if med > p50 * 0.4:               # 窗内有实际语音
                row["vol"] = float(np.percentile(rw, 95)) / p50
                pre = rms[max(0, j0 - int(2 / HOP)):j0]
                pre_med = float(np.median(pre[pre > 0])) if (pre > 0).any() \
                    else p50
                row["burst"] = float(rw.max()) / max(pre_med, 0.3 * p50)
        rows.append(row)

    ref_rows = [r for r in rows if r["host_frac"] > 0.5]
    refs = {k: np.array([r[k] for r in ref_rows])
            for k in ("vol", "burst", "spd", "emo_pm", "rep_pm")}
    base = {"rms_p50": p50, "rms_p40": float(np.percentile(r_host, 40)),
            "rms_p85": float(np.percentile(r_host, 85)),
            "rate_med": rate_med, "rate_ref": np.array(rates_all),
            "host_mask": host_mask}
    return rows, refs, base


def hype_of(rows, refs):
    """hype = 各窗口信号分位加权（自适应设备/音色，无固定阈值）。"""
    out = np.full(len(rows), 0.30)            # 无语音窗中性值
    for i, r in enumerate(rows):
        if r["host_frac"] <= 0.5:
            continue
        out[i] = (0.35 * _rank01(refs["vol"], r["vol"])
                  + 0.15 * _rank01(refs["burst"], r["burst"])
                  + 0.20 * _rank01(refs["spd"], r["spd"])
                  + 0.20 * _rank01(refs["emo_pm"], r["emo_pm"])
                  + 0.10 * _rank01(refs["rep_pm"], r["rep_pm"]))
    return out


# ---------------------------------------------------------------- 候选段吸附

def pick_segments(rows, hype, sentences, dur):
    """hype 阈值区间合并 -> 吸附到 host 句边界（一段完整的话）。"""
    times = np.array([r["t"] for r in rows])
    th = float(np.percentile(hype, HYPER_PCTL))
    idx = np.where(hype >= th)[0]
    groups = []
    for i in idx:
        if groups and times[i] - times[groups[-1][-1]] <= MERGE_GAP:
            groups[-1].append(int(i))
        else:
            groups.append([int(i)])
    segs = []
    for g in groups:
        t0 = times[g[0]] - WIN / 2
        t1 = times[g[-1]] + WIN / 2
        sn = _snap_sentences(t0, t1, sentences, times, hype)
        if sn:
            segs.append(sn)
    # 去重叠，按段内 hype 峰排序
    segs.sort(key=lambda s: -_seg_hype(s, times, hype))
    kept = []
    for s in segs:
        if all(s[1] <= k[0] or s[0] >= k[1] for k in kept):
            kept.append(s)
    return kept


def _snap_sentences(t0, t1, sentences, times, hype):
    """[t0,t1] -> 覆盖 host 句的完整边界；超长从峰向外收，过短向邻句扩。"""
    mid = [s for s in sentences
           if t0 <= (s["start"] + s["end"]) / 2 <= t1]
    if not mid:
        return None
    # 超长：以 hype 峰句为锚，按时间距收进来直到 <=MAX_SEG
    span = mid[-1]["end"] - mid[0]["start"]
    if span > MAX_SEG:
        m = (times >= t0) & (times <= t1)
        c = float(times[m][np.argmax(hype[m])])
        mid.sort(key=lambda s: abs((s["start"] + s["end"]) / 2 - c))
        kept_s, span = [], 0.0
        for s in mid:
            ns = [x for x in kept_s + [s]]
            sp = max(x["end"] for x in ns) - min(x["start"] for x in ns)
            if kept_s and sp > MAX_SEG:
                break
            kept_s.append(s)
            span = sp
        mid = sorted(kept_s, key=lambda s: s["start"])
    else:                                   # 过短：向邻句扩展
        while span < MIN_SEG:
            nxt = _nearest_outside(mid, sentences)
            if nxt is None:
                break
            mid.append(nxt)
            mid.sort(key=lambda s: s["start"])
            span = mid[-1]["end"] - mid[0]["start"]
    return mid[0]["start"], mid[-1]["end"] + TAIL_PAD


def _nearest_outside(mid, sentences):
    """离当前段边界最近的一句未选句。"""
    lo, hi = mid[0]["start"], mid[-1]["end"]
    cand = [s for s in sentences if s["end"] < lo or s["start"] > hi]
    if not cand:
        return None
    return min(cand, key=lambda s: min(abs(s["end"] - lo), abs(s["start"] - hi)))


def _seg_hype(seg, times, hype):
    m = (times >= seg[0]) & (times <= seg[1])
    return float(hype[m].max()) if m.any() else 0.0


# ---------------------------------------------------------------- 音频与基频

def _load_segment(audio_path, t0, t1):
    with wave.open(str(audio_path)) as w:
        w.setpos(max(0, int(t0 * SR)))
        return np.frombuffer(
            w.readframes(max(1, int((t1 - t0) * SR))),
            dtype=np.int16).astype(np.float32) / 32768.0


def _yin(seg):
    import librosa
    return librosa.yin(seg, fmin=65, fmax=500, sr=SR,
                       frame_length=1024, hop_length=256)


def f0_baseline(audio_path, rows, base):
    """场次基频中位：随机抽 F0_SAMPLES 个语音窗 4s 各跑 yin 取中位。"""
    rng = random.Random(11)
    cands = [r for r in rows
             if r["host_frac"] > 0.5 and r["vol"] > 0]
    if not cands:
        return 120.0
    f0_meds = []
    for r in rng.sample(cands, min(F0_SAMPLES, len(cands))):
        seg = _load_segment(audio_path, r["t"] - 2, r["t"] + 2)
        n = len(seg) // int(SR * HOP)
        r_ = np.sqrt((seg[:n * int(SR * HOP)].reshape(
            n, int(SR * HOP)) ** 2).mean(axis=1))
        f0 = _yin(seg)
        edges = np.linspace(0, len(f0), n + 1).astype(int)
        f0_r = np.array([np.median(f0[edges[i]:edges[i + 1]])
                         for i in range(n)])
        voiced = r_ > base["rms_p40"]
        if voiced.sum() < 4:
            continue
        f0_meds.append(float(np.median(f0_r[voiced])))
    return float(np.median(f0_meds)) if len(f0_meds) >= 8 else 120.0


# ---------------------------------------------------------------- 片段打分

def segment_signals(t0, t1, sentences, audio_path, rows, base, f0_base):
    """片段原始信号（不做归一）+ 证据句。返回 (signals, evidence, words)。

    段间可比性交给 dims_from_signals（候选段集合内分位），这里只算
    物理原始值：vol/burst 取段内窗口值的高分位/最大（爆发是瞬时事件），
    rate 取段内最快句语速，pitch/shout 用段内基频。
    """
    dur = t1 - t0                      # t1 已含 TAIL_PAD，文案按句算
    w = [r for r in rows if t0 <= r["t"] <= t1 and r["host_frac"] > 0.5]
    vols = sorted(r["vol"] for r in w)
    vol = vols[int(len(vols) * 0.9)] if vols else None      # p90，抗单窗毛刺
    burst = max((r["burst"] for r in w), default=None)
    cov = [s for s in sentences if t0 <= (s["start"] + s["end"]) / 2 <= t1]

    seg = _load_segment(audio_path, t0, t1)
    n = len(seg) // int(SR * HOP)
    r_ = np.sqrt((seg[:n * int(SR * HOP)].reshape(
        n, int(SR * HOP)) ** 2).mean(axis=1))
    m = base["host_mask"][int(t0 / HOP):int(t0 / HOP) + n][:len(r_)]
    m = m & (r_ > 0) & (r_ > base["rms_p40"] * 0.5)

    f0 = _yin(seg)
    edges = np.linspace(0, len(f0), n + 1).astype(int)
    f0_r = np.array([np.median(f0[edges[i]:edges[i + 1]])
                     for i in range(n)])
    voiced = m & (r_ > base["rms_p40"])
    pitch_up, shout = None, None
    if voiced.sum() >= 4:
        pitch_up = float(np.median(f0_r[voiced])) / f0_base - 1.0
        shout_m = voiced & (r_ > base["rms_p85"]) \
            & (f0_r > 1.15 * f0_base)
        shout = float(shout_m.sum() / voiced.sum())

    rates = [len((s.get("text") or "").replace(" ", "")) /
             max(0.5, s["end"] - s["start"])
             for s in cov if s["end"] - s["start"] >= 1.0]
    rate = max(rates) / base["rate_med"] if rates else None
    chains, cur, prev_end = [], 0.0, None
    for s in cov:
        if prev_end is not None and s["start"] - prev_end <= GAP_SPEECH:
            cur += s["end"] - s["start"]
        else:
            chains.append(cur)
            cur = s["end"] - s["start"]
        prev_end = s["end"]
    chains.append(cur)
    dense = max(chains) if chains else 0.0
    emo_n = sum(_count_emotion(s.get("text") or "") for s in cov)
    rep_n = sum(_count_repeat(s.get("text") or "") for s in cov)

    sig = {
        "peak_ratio": None if vol is None else round(vol, 2),
        "burst": None if burst is None else round(burst, 1),
        "pitch_up": None if pitch_up is None else round(pitch_up, 3),
        "shout": None if shout is None else round(shout, 3),
        "rate": None if rate is None else round(rate, 2),
        "dense_sec": round(float(dense), 1),
        "emo_words": emo_n, "repeats": rep_n, "sent_n": len(cov),
        "_emo_pm": emo_n * 60 / dur, "_rep_pm": rep_n * 60 / dur,
    }
    evidence = [{"t": round(s["start"] - t0, 1),
                 "text": (s.get("text") or "").strip()}
                for s in cov]
    words = sorted({w_ for w_ in EMOTION_WORDS if w_ not in _SINGLE
                    and any(w_ in (s.get("text") or "") for s in cov)})
    return sig, evidence, words


# 维度 <-> 原始信号键（段间分位用）
_DIM_SIGNAL = [
    ("声强V", "peak", "peak_ratio"), ("声强V", "burst", "burst"),
    ("音调T", "pitch", "pitch_up"), ("音调T", "shout", "shout"),
    ("语流F", "rate", "rate"), ("语流F", "dense", "dense_sec"),
    ("语义W", "emotion", "_emo_pm"), ("语义W", "repeat", "_rep_pm"),

]


def dims_from_signals(cands):
    """候选段集合内逐信号分位 -> 8 维分。

    设备增益/音色/语速习惯各场不同，候选段互相排名即自适应。
    分位保底 0.3（对齐击倒打分「信号缺席走中性值」的约定）：如词表
    命不中的场次语义维全场同值，惩罚物理上激动的段没有意义。
    """
    dims = [{"声强V": {}, "音调T": {}, "语流F": {}, "语义W": {}}
            for _ in cands]
    for group, key, sig_key in _DIM_SIGNAL:
        vals = [c["signals"][sig_key] for c in cands]
        ref = [v for v in vals if v is not None]
        for i, v in enumerate(vals):
            dims[i][group][key] = round(
                min(1.0, max(0.3, _rank01(ref, v))), 3)
    return dims


# ---------------------------------------------------------------- 主入口

def voice_session(target, config=None, top=30, force=False):
    """单场次链路：粗扫 -> 候选段吸附句界 -> 精算打分 -> 切音频。"""
    config = config or load_config()
    meta, sents, audio = resolve_asr(target)
    dur = float(meta.get("duration_s") or 0)
    if not dur and sents:
        dur = float(sents[-1]["end"])
    if not dur:
        raise RuntimeError("transcript 无 duration_s，无法定位")
    name = _slug(Path(meta.get("game") or target).stem, 40) or "voice"
    out_dir = DATA / "voice" / _slug(
        Path(target).name if Path(target).is_dir()
        else Path(target).parent.name, 40)
    print(f"[{name}] 时长 {dur / 60:.0f}min，host 句 {len(sents)}")

    print("  L1 窗口信号 ...")
    _, rms = rms_timeline(audio)
    chars, emo, rep = char_bins(sents, dur)
    rows, refs, base = window_signals(rms, chars, emo, rep, sents, dur)
    print(f"  L2 基频基线（抽样 {F0_SAMPLES} 窗）...")
    f0_base = f0_baseline(audio, rows, base)
    print(f"    场次基频 {f0_base:.0f}Hz")

    hype = hype_of(rows, refs)
    segs = pick_segments(rows, hype, sents, dur)
    print(f"  候选段 {len(segs)} 个（hype p{HYPER_PCTL} 阈值）")
    if not segs:
        print("  （无候选段）")
        return out_dir

    scored = []
    for t0, t1 in segs[:top * 2]:
        sig, evidence, words = segment_signals(
            t0, t1, sents, audio, rows, base, f0_base)
        scored.append({"t_start": round(t0, 1), "t_end": round(t1, 1),
                       "signals": sig, "evidence": evidence,
                       "words": words})
    for c, d in zip(scored, dims_from_signals(scored)):
        c["dims"] = d
        total = sum(W_GROUP[g] * sum(dd.values()) / 2
                    for g, dd in d.items()) * 100
        c["score"] = round(min(100.0, total), 1)
        c["grade"] = _grade(total)
    scored.sort(key=lambda x: -x["score"])

    out_dir.mkdir(parents=True, exist_ok=True)
    kept = []
    for i, r in enumerate(scored[:top], 1):
        t0, t1 = r["t_start"], r["t_end"]
        first = (r["evidence"] or [{}])[0].get("text", "")
        label = _slug(first[:26] or "voice", 26)
        out = out_dir / f"{i:02d}_{_mmss(t0)}_{label}.m4a"
        if out.exists() and not force:
            print(f"    [{i:02d}] 已存在，跳过: {out.name}")
        else:
            _cut_audio(audio, t0, t1, out)
        r["file"] = out.name
        r["cut"] = [t0, t1]
        kept.append(r)
        s = r["signals"]
        print(f"    [{i:02d}] {r['grade']} {r['score']:5.1f} "
              f"{_mmss(t0)} {t1 - t0:>4.0f}s {s['sent_n']}句 "
              f"峰{s['peak_ratio']}× 调+{s['pitch_up']} "
              f"速{s['rate']}× 词{s['emo_words']} | {first[:20]}")

    index = {"session": name, "audio": str(audio), "out_dir": str(out_dir),
             "f0_base": f0_base, "clips": kept}
    (out_dir / "voice_scores.json").write_text(
        json.dumps(index, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"voice_scores.json 已写入 {out_dir}")
    return out_dir


def _cut_audio(audio_path, t0, t1, out):
    # -ss 置于 -i 前（输入 seek，wav 无损），配 -t 时长（-to 会变相对语义）
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
                    "-ss", f"{t0:.2f}", "-i", str(audio_path),
                    "-t", f"{t1 - t0:.2f}", "-c:a", "aac", "-b:a", "96k",
                    str(out)], check=True)


# ---------------------------------------------------------------- HTML

_AXES = [("声强V", "peak", "音量峰"), ("声强V", "burst", "爆发"),
         ("音调T", "pitch", "升调"), ("音调T", "shout", "喊声"),
         ("语流F", "rate", "语速"), ("语流F", "dense", "连讲"),
         ("语义W", "emotion", "情绪词"), ("语义W", "repeat", "叠字")]


def render_html():
    """汇总所有已有 voice_scores.json 的场次，生成 data/voice/review.html。"""
    sessions = []
    for p in sorted((DATA / "voice").glob("*/voice_scores.json")):
        d = json.loads(p.read_text(encoding="utf-8"))
        sess_dir = p.parent.name
        for c in d["clips"]:
            c["video_src"] = f"{sess_dir}/{c['file']}"
        sessions.append(d)
    if not sessions:
        raise RuntimeError("没有任何 voice_scores.json，先跑 voice")
    payload = json.dumps(sessions, ensure_ascii=False).replace("</", "<\\/")
    html = _HTML_TEMPLATE.replace("__DATA__", payload)
    out = DATA / "voice" / "review.html"
    out.write_text(html, encoding="utf-8")
    n = sum(len(s["clips"]) for s in sessions)
    print(f"review 页已生成（{len(sessions)} 场次 / {n} 片段）: {out}")
    return out


_HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="zh">
<head>
<meta charset="utf-8">
<title>主播语音情绪 review</title>
<style>
:root{--bg:#101418;--card:#1a2027;--line:#2a323c;--fg:#dce3ea;--dim:#8b98a5;
--good:#4cc38a;--mid:#e5c454;--bad:#e5534b;--s:#ff7b54}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);
font:14px/1.6 -apple-system,"PingFang SC","Microsoft YaHei",sans-serif}
header{position:sticky;top:0;z-index:9;background:rgba(16,20,24,.96);
border-bottom:1px solid var(--line);padding:10px 18px;display:flex;
gap:14px;align-items:center;flex-wrap:wrap}
header h1{font-size:16px;margin:0 10px 0 0}
header select,header button{background:var(--card);color:var(--fg);
border:1px solid var(--line);border-radius:6px;padding:4px 10px}
header label{color:var(--dim);cursor:pointer}
main{max-width:1180px;margin:0 auto;padding:16px}
.card{background:var(--card);border:1px solid var(--line);border-radius:10px;
padding:14px 16px;margin-bottom:14px;display:grid;grid-template-columns:
300px 1fr 190px;gap:16px}
audio{width:300px}
.badge{display:inline-block;border-radius:5px;padding:0 7px;font-weight:700}
.g-S{background:var(--s);color:#1a1208}.g-A{background:var(--good);color:#06180e}
.g-B{background:var(--mid);color:#1c1503}.g-C{background:var(--line);color:var(--dim)}
.score{font-size:26px;font-weight:800}
.title{font-weight:600;margin:2px 0 6px}
.meta{color:var(--dim);font-size:12px}
.dims{display:grid;grid-template-columns:1fr 1fr;gap:3px 18px;margin-top:8px}
.dim{display:grid;grid-template-columns:44px 1fr 34px;gap:8px;align-items:center}
.bar{height:6px;border-radius:3px;background:var(--line);overflow:hidden}
.bar>i{display:block;height:100%;border-radius:3px;background:var(--good)}
.dim span{font-size:12px;color:var(--dim)}
.dim b{font-size:12px;text-align:right}
.sig{color:var(--dim);font-size:12px;margin-top:6px}
.ev{margin:6px 0 0;padding:0;list-style:none;max-height:150px;overflow:auto}
.ev li{font-size:12px;color:var(--fg);cursor:pointer;padding:1px 0}
.ev li:hover{color:var(--good)}
.ev li::before{content:"▸ ";color:var(--dim)}
.chips{margin-top:6px}.chip{display:inline-block;font-size:11px;color:var(--s);
border:1px solid var(--s);border-radius:4px;padding:0 6px;margin:0 4px 3px 0}
.vote{display:flex;flex-direction:column;gap:8px;align-items:center}
.vote .btns{display:flex;gap:8px}
.vote button{font-size:18px;width:44px;height:38px;border-radius:8px;cursor:pointer;
background:var(--bg);border:1px solid var(--line)}
.vote button.on-up{background:var(--good);color:#04150c}
.vote button.on-down{background:var(--bad);color:#fff}
h2.sess{margin:22px 0 10px;font-size:15px;color:var(--dim);
border-bottom:1px solid var(--line);padding-bottom:6px}
.dimnote{color:var(--dim);font-size:11px;margin-top:4px}
</style>
</head>
<body>
<header>
<h1>🎙 主播语音情绪 review</h1>
<select id="sess"></select>
<label>最低分 <input type="number" id="minsc" value="0" style="width:52px"></label>
<button id="exp">导出 labels.json</button>
<span id="stat" class="meta"></span>
</header>
<main id="list"></main>
<script>
const DATA = __DATA__;
const AXES = [["声强V","peak","音量峰"],["声强V","burst","爆发"],
["音调T","pitch","升调"],["音调T","shout","喊声"],
["语流F","rate","语速"],["语流F","dense","连讲"],
["语义W","emotion","情绪词"],["语义W","repeat","叠字"]];
const LBL = "voice_review_labels";
let labels = JSON.parse(localStorage.getItem(LBL) || "{}");
const $ = s => document.querySelector(s);
const esc = s => String(s).replace(/[&<>"]/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));
function grade(s){return s>=78?"S":s>=64?"A":s>=48?"B":"C"}

function radar(cv, vals){
  const ctx = cv.getContext("2d"), W=cv.width, H=cv.height;
  const cx=W/2, cy=H/2+4, R=W/2-22, n=vals.length;
  ctx.clearRect(0,0,W,H);
  ctx.strokeStyle="#2a323c"; ctx.fillStyle="none";
  for(const f of [0.5,1]){
    ctx.beginPath();
    for(let i=0;i<=n;i++){const a=Math.PI*2*i/n-Math.PI/2, r=R*f;
      const x=cx+r*Math.cos(a), y=cy+r*Math.sin(a);
      i?ctx.lineTo(x,y):ctx.moveTo(x,y);}
    ctx.stroke();
  }
  ctx.beginPath();
  for(let i=0;i<=n;i++){const a=Math.PI*2*(i%n)/n-Math.PI/2, r=R*vals[i%n];
    const x=cx+r*Math.cos(a), y=cy+r*Math.sin(a);
    i?ctx.lineTo(x,y):ctx.moveTo(x,y);}
  ctx.closePath(); ctx.fillStyle="rgba(76,195,138,.30)";
  ctx.strokeStyle="#4cc38a"; ctx.fill(); ctx.stroke();
  ctx.fillStyle="#8b98a5"; ctx.font="10px sans-serif";
  ctx.textAlign="center"; ctx.textBaseline="middle";
  AXES.forEach((a,i)=>{const ang=Math.PI*2*i/n-Math.PI/2;
    ctx.fillText(a[2], cx+(R+12)*Math.cos(ang), cy+(R+12)*Math.sin(ang));});
}

function dimRows(c){
  return AXES.map(([g,k,label])=>{
    const v=(c.dims[g]||{})[k]||0;
    const pct=Math.round(v*100);
    const col=v>=0.7?"var(--good)":v>=0.4?"var(--mid)":"var(--bad)";
    return `<div class="dim"><span>${label}</span>`+
      `<div class="bar"><i style="width:${pct}%;background:${col}"></i></div>`+
      `<b>${(v).toFixed(2)}</b></div>`;
  }).join("");
}

function sigLine(c){
  const s=c.signals, out=[];
  if(s.peak_ratio!=null) out.push(`音量峰 ${s.peak_ratio}×基线`);
  if(s.burst!=null) out.push(`爆发 ${s.burst}×`);
  if(s.pitch_up!=null) out.push(`升调 +${Math.round(s.pitch_up*100)}%`);
  if(s.shout!=null) out.push(`喊声 ${Math.round(s.shout*100)}%`);
  if(s.rate!=null) out.push(`语速 ${s.rate}×`);
  if(s.dense_sec!=null) out.push(`连讲 ${s.dense_sec}s`);
  if(s.sent_n!=null) out.push(`${s.sent_n} 句`);
  out.push(`情绪词 ${s.emo_words||0} / 叠字 ${s.repeats||0}`);
  return out.join(" · ");
}

function card(c){
  const g=grade(c.score), v=labels[c.file]||{};
  const chips=(c.words||[]).map(w=>`<span class="chip">${esc(w)}</span>`).join("");
  const src = encodeURI(c.video_src);
  const mmss=t=>{const m=Math.floor(t/60),s=Math.round(t%60);
    return `${String(m).padStart(2,"0")}:${String(s).padStart(2,"0")}`};
  return `<div class="card" id="${esc(c.file)}">
  <div style="padding-top:6px">
    <audio controls preload="none"><source src="${src}" type="audio/mp4"></audio>
    <div class="meta" style="margin-top:6px">源 ${mmss(c.cut[0])} 起 ｜
      ${Math.round(c.cut[1]-c.cut[0])}s</div>
  </div>
  <div>
    <div class="title"><span class="badge g-${g}">${g}</span>
      <span class="score" style="margin-left:8px">${c.score.toFixed(1)}</span>
      <span class="meta">${esc(c.file)}</span></div>
    <div class="dims">${dimRows(c)}</div>
    <div class="sig">${sigLine(c)}</div>
    <ul class="ev">${(c.evidence||[]).map(e=>
      `<li data-t="${e.t}">${esc(e.text)}</li>`).join("")}</ul>
    <div class="chips">${chips}</div>
  </div>
  <div class="vote">
    <canvas width="164" height="150" class="radar"></canvas>
    <div class="btns">
      <button class="up ${v.v=="up"?"on-up":""}" data-f="${esc(c.file)}">👍</button>
      <button class="down ${v.v=="down"?"on-down":""}" data-f="${esc(c.file)}">👎</button>
    </div>
  </div>
</div>`;
}

function render(){
  const sess=$("#sess").value, min=+$("#minsc").value||0;
  const list=$("#list");
  // 先攒再一次性 innerHTML：循环 += 会反复重建整棵树，
  // <audio> 冷加载中途被销毁会停在 error 态（用户实测"无法播放媒体"）
  const parts=[];
  let total=0;
  for(const s of DATA){
    if(sess!=="all" && s.session!==sess) continue;
    const clips=s.clips.filter(c=>c.score>=min).sort((a,b)=>b.score-a.score);
    if(!clips.length) continue;
    total+=clips.length;
    parts.push(`<h2 class="sess">${esc(s.session)}（${clips.length}）</h2>`);
    for(const c of clips) parts.push(card(c));
  }
  list.innerHTML=parts.join("");
  $("#stat").textContent=`共 ${total} 片段 ｜ 👍 ${
    Object.values(labels).filter(x=>x.v=="up").length} ｜ 👎 ${
    Object.values(labels).filter(x=>x.v=="down").length}`;
  document.querySelectorAll(".card").forEach(el=>{
    const cv=el.querySelector(".radar"), c=DATA.flatMap(s=>s.clips).find(x=>x.file===el.id);
    radar(cv, AXES.map(([g,k])=>((c.dims[g]||{})[k]||0)));
  });
  document.querySelectorAll(".vote button").forEach(b=>b.onclick=()=>{
    const f=b.dataset.f, cur=labels[f]||{};
    const nv=cur.v===(b.classList.contains("up")?"up":"down")?null:
      (b.classList.contains("up")?"up":"down");
    if(nv) labels[f]={v:nv}; else delete labels[f];
    localStorage.setItem(LBL, JSON.stringify(labels)); render();
  });
  document.querySelectorAll(".ev li").forEach(li=>li.onclick=()=>{
    const au=li.closest(".card").querySelector("audio");
    au.currentTime=parseFloat(li.dataset.t); au.play();
  });
}

const sel=$("#sess");
sel.innerHTML=`<option value="all">全部场次</option>`+
  DATA.map(s=>`<option>${esc(s.session)}</option>`).join("");
sel.onchange=render; $("#minsc").oninput=render;
$("#exp").onclick=()=>{
  const a=document.createElement("a");
  a.href=URL.createObjectURL(new Blob(
    [JSON.stringify(labels,null,1)],{type:"application/json"}));
  a.download="labels.json"; a.click();
};
render();
</script>
</body>
</html>
"""
