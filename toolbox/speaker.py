"""说话人标注：对 ASR 转写句子按音色聚类，区分主播与游戏播报/唱歌等其他音源。

方法（2026-09-09 在三角洲直播素材上试验验证）：
- 用 transcript 的句子边界（3-15s，比 VAD 段长而稳）从 16k 音频切句提声纹
  （ModelScope CAM++，192 维，绕开不可达的 HuggingFace）；
- 余弦距离层次聚类（distance_threshold=0.65）；最大簇（按总时长）= 主播
  host，其余簇 = other（实测 TTS 播报/唱歌独立成簇）；
- <1.5s 短句声纹不稳，按时间上最近的长句标签归属。

已知局限：队友语音与主播重叠时被主播声纹主导（KOOK 音量低），不会独立
成簇——"other" 主要是 TTS/播报/唱歌，不要当作队友检测器用。

用法（CLI）：python3 -m toolbox speaker data/asr/p2/transcript.json
输出：同目录 speaker_labeled.json（原句不变，新增 speaker 字段）。
"""
import json
import wave
from pathlib import Path

import numpy as np

_MODEL = None


def _get_model():
    global _MODEL
    if _MODEL is None:
        from modelscope.pipelines import pipeline
        _MODEL = pipeline(
            task="speaker-verification",
            model="iic/speech_campplus_sv_zh-cn_16k-common").model
        _MODEL.eval()
    return _MODEL


def _load_audio(audio_path):
    with wave.open(str(audio_path)) as w:
        sr = w.getframerate()
        data = np.frombuffer(w.readframes(w.getnframes()),
                             dtype=np.int16).astype(np.float32) / 32768.0
    return sr, data


def extract_audio(video_path, out_wav):
    """视频 → 16k 单声道 wav（无现成音频时用）。"""
    import subprocess
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error",
                    "-i", str(video_path), "-vn", "-ac", "1", "-ar", "16000",
                    "-acodec", "pcm_s16le", "-y", str(out_wav)], check=True)


def label_speakers(transcript_path, audio_path=None, threshold=0.65,
                   min_dur=1.5):
    """标注句子说话人，返回 (sentences, 摘要)。

    sentences：原句 dict 列表，新增 speaker ∈ {"host", "other"}。
    audio_path 缺省取 transcript 同目录 audio_16k.wav。
    """
    import torch
    from sklearn.cluster import AgglomerativeClustering

    transcript_path = Path(transcript_path)
    data = json.loads(transcript_path.read_text(encoding="utf-8"))
    sentences = data["sentences"] if isinstance(data, dict) else data

    if audio_path is None:
        audio_path = transcript_path.parent / "audio_16k.wav"
    audio_path = Path(audio_path)
    if not audio_path.exists():
        raise FileNotFoundError(
            f"音频不存在: {audio_path}（先跑 ASR 或用 --audio/--video 指定）")

    sr, audio = _load_audio(audio_path)
    model = _get_model()

    embs, idx_long = [], []
    for i, s in enumerate(sentences):
        seg = audio[int(s["start"] * sr):int(s["end"] * sr)]
        if len(seg) < sr * min_dur:
            continue
        with torch.no_grad():
            e = model(torch.from_numpy(seg[None, :]))
        embs.append(e[0].numpy())
        idx_long.append(i)
    if len(embs) < 2:
        for s in sentences:
            s["speaker"] = "host"      # 样本太少，全记 host
        return sentences, {"clusters": 1, "host_sec": 0, "note": "句样本不足"}

    embs = np.array(embs)
    embs = embs / np.linalg.norm(embs, axis=1, keepdims=True)
    cl = AgglomerativeClustering(n_clusters=None, distance_threshold=threshold,
                                 metric="cosine", linkage="average")
    labels = cl.fit_predict(embs)

    # 最大簇（按语音总时长）= 主播
    import collections
    dur = collections.defaultdict(float)
    for i, lb in zip(idx_long, labels):
        s = sentences[i]
        dur[lb] += s["end"] - s["start"]
    host_lb = max(dur, key=dur.get)
    for i, lb in zip(idx_long, labels):
        sentences[i]["speaker"] = "host" if lb == host_lb else "other"

    # 短句：按最近的长句标签归属
    last = "host"
    for i, s in enumerate(sentences):
        if "speaker" in s:
            last = s["speaker"]
        else:
            s["speaker"] = last

    host_sec = sum(s["end"] - s["start"] for s in sentences
                   if s["speaker"] == "host")
    summary = {"clusters": len(set(labels)), "host_lb": int(host_lb),
               "host_sec": round(host_sec, 1),
               "other_sec": round(sum(s["end"] - s["start"] for s in sentences
                                      if s["speaker"] == "other"), 1)}
    return sentences, summary


def run(transcript_path, audio_path=None, video_path=None, threshold=0.65):
    """完整流程：标注并写出 speaker_labeled.json，返回输出路径与摘要。"""
    transcript_path = Path(transcript_path)
    if audio_path is None and video_path:
        audio_path = transcript_path.parent / "audio_16k.wav"
        if not audio_path.exists():
            extract_audio(video_path, audio_path)
    sentences, summary = label_speakers(transcript_path, audio_path,
                                        threshold=threshold)
    out = transcript_path.parent / "speaker_labeled.json"
    out.write_text(json.dumps(
        {"source": str(transcript_path), "threshold": threshold,
         "summary": summary, "sentences": sentences},
        ensure_ascii=False, indent=1), encoding="utf-8")
    return out, summary
