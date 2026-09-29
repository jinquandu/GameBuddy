"""击倒片段链路：事件提取（detect）→ 裁剪所有击倒片段至一个文件夹。

输入支持：场次目录（含 P1..PN 分片）/ 单个视频文件 / 已登记 video_id。
对每个分片：
1. 经 toolbox.events 取该源事件流（复用 data/reports/ 已有，或现场检测）；
2. 过滤 kind=down 事件，相邻击倒（间隔 < merge_sec）合并为一段（连杀）；
3. 按 [首杀-pre, 末杀+post] 用 ffmpeg 裁剪（编码参数同 highlight），
   统一编号输出到 data/knockdowns/<场次名>/，附 knockdowns.json 索引。

素材收集/事件池获取/命名约定已下沉 toolbox.events / toolbox.naming
（2026-09-22），本模块只保留击倒领域逻辑；下方转引仅为兼容历史导入方
（服务端镜像包可能仍 `from toolbox.knockdown import ...`）。
"""
import json
import subprocess
from pathlib import Path

from toolbox.config import DATA, load_config
from toolbox.detector import _video_duration
# collect_videos/events_for 兼容转引：历史版本寄生在本模块，新代码请从
# events/naming 导入
from toolbox.events import collect_videos, events_for, part_no
from toolbox.naming import mmss as _mmss, slug as _slug


def merge_downs(events, merge_sec):
    """击倒事件按时间排序、相邻合并（连杀），返回 [{t0, t1, downs:[event]}]。"""
    downs = sorted((e for e in events if e.get("kind") == "down"),
                   key=lambda e: e["t_start"])
    segs = []
    for e in downs:
        if segs and e["t_start"] - segs[-1]["t1"] <= merge_sec:
            g = segs[-1]
            g["t1"] = e["t_start"]
            g["downs"].append(e)
        else:
            segs.append({"t1": e["t_start"], "downs": [e]})
    for g in segs:
        g["t0"] = g["downs"][0]["t_start"]
    return segs


def _cut(src, t0, t1, out: Path):
    out.parent.mkdir(parents=True, exist_ok=True)
    # -threads 4：pickups 并行裁剪 4 进程时编码线程总量 = 16，不超订；
    # 串行调用（knockdowns 单场 5 片）实测影响可忽略
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
                    "-ss", f"{t0:.2f}", "-to", f"{t1:.2f}", "-i", str(src),
                    "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
                    "-threads", "4",
                    "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "160k",
                    "-movflags", "+faststart", str(out)], check=True)


def knockdowns(target, config=None, pre=None, post=None, merge=None,
               redetect=False, fps=1.0, workers=None):
    """链路入口：事件提取 -> 过滤击倒 -> 裁剪到一个文件夹，返回产出目录。"""
    config = config or load_config()
    kd = config.get("knockdown", {})
    pre = kd.get("pre_sec", 6.0) if pre is None else pre
    post = kd.get("post_sec", 10.0) if post is None else post
    merge = kd.get("merge_sec", 12.0) if merge is None else merge

    videos = collect_videos(target)
    session = (videos[0].parent.name if len(videos) > 1 or Path(target).is_dir()
               else videos[0].stem)
    out_dir = DATA / "knockdowns" / _slug(session, 60)
    index = {"session": session, "clips": []}

    seq = 0
    for video in videos:
        events = events_for(video, config, redetect=redetect, fps=fps,
                            workers=workers)
        segs = merge_downs(events, merge)
        part = part_no(video)
        duration = _video_duration(video)
        n_down = sum(len(s["downs"]) for s in segs)
        print(f"  {video.name[:36]}：{n_down} 个击倒 -> {len(segs)} 段")
        for g in segs:
            t0, t1 = max(0.0, g["t0"] - pre), min(duration, g["t1"] + post)
            targets = [d.get("meta", {}).get("target") or "?" for d in g["downs"]]
            label = (f"击倒_{targets[0]}" if len(targets) == 1
                     else f"击倒x{len(targets)}_{targets[0]}等")
            seq += 1
            out = out_dir / f"{seq:02d}_P{part}_{_mmss(g['t0'])}_{_slug(label, 44)}.mp4"
            _cut(video, t0, t1, out)
            index["clips"].append({
                "file": out.name, "part": part, "t_start": g["t0"],
                "t_end": g["t1"], "cut": [round(t0, 2), round(t1, 2)],
                "targets": targets,
                "events": [{
                    "t": d["t_start"], "detail": d.get("detail"),
                    # 爆头信号：击杀信息流原文含「击中头部」（1fps 采样可能漏读，
                    # 读到即真；评分侧视为充分不必要信号）
                    "headshot": "头" in ((d.get("meta") or {}).get("text") or ""),
                    "text": (d.get("meta") or {}).get("text") or d.get("detail"),
                } for d in g["downs"]]})
            print(f"    [{seq:02d}] P{part} {_mmss(g['t0'])} {label} "
                  f"({t1 - t0:.0f}s)")

    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "knockdowns.json").write_text(
        json.dumps(index, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"共 {seq} 个击倒片段 + knockdowns.json 已写入 {out_dir}")
    return out_dir
