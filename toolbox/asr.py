"""ASR 转写（应用端 · 流程图节点 2）。

funasr paraformer-zh + fsmn-vad + ct-punc 本地转写，免 key；模型从
ModelScope 自动下载（~1GB，缓存 ~/.cache/modelscope）。逐句时间戳是
高光切片/说话人标注/语音情绪三条线的公共锚点，句界由此产出、下游只对齐不改时间。

产物契约（与既有 data/asr/<场次>/ 完全一致，旧数据可直接复用）：
    data/asr/<场次slug>/transcript.json  {game, model, duration_s, sentences:[{id,start,end,text}]}
    data/asr/<场次slug>/audio_16k.wav    16k 单声道（speaker/voice 链路消费）

移植自 bilibili-highlight-extractor skill 的 transcribe.py（分句算法原样保留）。
"""
import json
import re
import subprocess
from pathlib import Path

from toolbox.config import ASR


def _slug(s, n=48):
    s = re.sub(r"[^\w\u4e00-\u9fff]+", "_", s).strip("_")
    return (s or "asr")[:n]


def to_wav16k(video: Path, out: Path):
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-i", str(video),
                    "-vn", "-ac", "1", "-ar", "16000", "-acodec", "pcm_s16le",
                    "-y", str(out)], check=True)


# ---------------------------------------------------------------- 分句（与 skill 版一致）

def _f(v):  # ms -> s (funasr 用 ms)
    return (v or 0) / 1000.0


_SENT_END = set("。！？!?")


def _flush(cur, sents):
    times = [t for _, t in cur if t is not None]
    if not times:
        return
    sents.append({"start": times[0][0] / 1000.0, "end": times[-1][1] / 1000.0,
                  "text": "".join(ch for ch, _ in cur).strip()})


def extract_sentences(res):
    """funasr 返回 {text(带标点), timestamp(逐字符 [start_ms,end_ms])}。
    逐字符走文本：字母数字消耗下一个时间戳、标点不消耗；句读符（。！？!?）
    触发 flush，句子 start/end 取首尾字符时间戳——每句对齐真实音频边界，
    这正是下游切片吸附所依赖的。"""
    r = res[0] if isinstance(res, list) and res else (res if isinstance(res, dict) else {})
    text = (r.get("text") or "") if isinstance(r, dict) else ""
    ts = (r.get("timestamp") or []) if isinstance(r, dict) else []
    sents = []
    if isinstance(r, dict) and r.get("sentences"):     # 未来版本提供显式句子字段则优先
        for s in r["sentences"]:
            sents.append({"start": _f(s.get("start")), "end": _f(s.get("end")),
                          "text": (s.get("text") or "").strip()})
    if not sents and text and ts:
        cur = []
        ti = 0
        for c in text:
            if c.isalnum():
                cur.append((c, ts[ti] if ti < len(ts) else None))
                ti += 1
            else:
                cur.append((c, None))
                if c in _SENT_END:
                    _flush(cur, sents)
                    cur = []
        if cur:
            _flush(cur, sents)
    if text and ti != len(ts) and not sents:
        # 对齐失败的兜底：整段一句话（降级，无逐句吸附）
        sents = [{"start": _f(ts[0][0]), "end": _f(ts[-1][1]),
                  "text": text.strip()}] if ts else []
    return [s for s in sents if s["text"]]


# ---------------------------------------------------------------- 链路

def find_transcript_dir(video: Path):
    """按 game 字段找该视频已转写的 asr 目录，无则 None（幂等跳过用）。"""
    if not ASR.exists():
        return None
    for tpath in sorted(ASR.glob("*/transcript.json")):
        try:
            data = json.loads(tpath.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            continue
        if data.get("game") == str(video):
            return tpath.parent
    return None


def _load_model():
    try:
        from funasr import AutoModel
    except ImportError as e:
        raise RuntimeError(
            "未安装 funasr（应用端依赖）：pip install -r requirements-client.txt") from e
    return AutoModel(model="paraformer-zh",
                     vad_model="fsmn-vad",
                     vad_kwargs={"max_single_segment_time": 30000},
                     punc_model="ct-punc")


def transcribe_part(video: Path, model, out_root: Path = None):
    """转写单个分片 -> data/asr/<slug>/（已存在同 game 转写则跳过）。
    返回 (asr_dir, 句数)。"""
    video = video.resolve()
    out_root = out_root or ASR
    if find_transcript_dir(video):
        d = find_transcript_dir(video)
        n = len(json.loads((d / "transcript.json").read_text(encoding="utf-8"))
                .get("sentences", []))
        print(f"  {video.name[:40]}：已有转写，跳过（{d.name}，{n} 句）")
        return d, n

    out_dir = out_root / _slug(video.stem)
    while True:                       # 目录名撞车且不属于本视频时加序号
        if not out_dir.exists() or find_transcript_dir(video):
            break
        out_dir = out_dir.parent / (out_dir.name + "_2")
    out_dir.mkdir(parents=True, exist_ok=True)

    print(f"  {video.name[:40]}：提取 16k 音频 ...", flush=True)
    wav = out_dir / "audio_16k.wav"
    to_wav16k(video, wav)

    print(f"  {video.name[:40]}：paraformer 转写中（30s 分段经 VAD 批量）...", flush=True)
    res = model.generate(input=str(wav), batch_size_s=300)
    sents = extract_sentences(res)
    for i, s in enumerate(sents):
        s["id"] = i
    dur = sents[-1]["end"] if sents else 0.0
    (out_dir / "transcript.json").write_text(
        json.dumps({"game": str(video), "model": "paraformer-zh",
                    "duration_s": dur, "sentences": sents},
                   ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"  {video.name[:40]}：{len(sents)} 句，末句 {dur:.1f}s -> {out_dir.name}")
    for s in sents[:4]:
        print(f"    [{s['start']:7.1f}-{s['end']:7.1f}] {s['text'][:50]}")
    return out_dir, len(sents)


def asr(target):
    """链路入口：场次目录（P1..PN 分片）/ 单个视频文件 -> 逐分片转写。
    模型只加载一次，多分片复用。返回本轮新转写的 asr 目录列表。"""
    from toolbox.events import collect_videos
    videos = collect_videos(target)
    print(f"ASR 转写：{len(videos)} 个分片（模型加载一次，逐分片转写）")
    model = _load_model()
    done = []
    for v in videos:
        d, _ = transcribe_part(v, model)
        done.append(d)
    return done
