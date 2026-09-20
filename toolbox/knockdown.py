"""击倒片段链路：事件提取（detect）→ 裁剪所有击倒片段至一个文件夹。

输入支持：场次目录（含 P1..PN 分片）/ 单个视频文件 / 已登记 video_id。
对每个分片：
1. 复用 data/reports/ 下已有的该源事件流（按事件 src 匹配）；没有、或事件
   过稀（<min-events 视为上次检测未跑完）、或 --redetect 时，现场跑
   detector.scan_video 并按 <分片名>_full 约定写入事件池；
2. 过滤 kind=down 事件，相邻击倒（间隔 < merge_sec）合并为一段（连杀）；
3. 按 [首杀-pre, 末杀+post] 用 ffmpeg 裁剪（编码参数同 highlight），
   统一编号输出到 data/knockdowns/<场次名>/，附 knockdowns.json 索引。
"""
import json
import re
import subprocess
from pathlib import Path

from toolbox.config import DATA, REPORTS, load_config
from toolbox.detector import _video_duration, scan_video, write_jsonl

# 事件流是「扫完一次性写出」（write-once），存在的文件即为完整结论；跑刀
# 等安静分片全片仅个位数事件是常态。旧阈值 20 会把这类分片误判为未扫完而
# 整段重扫（2026-09-20 本机 3 个分片白扫 ~90min，同 trap 见 pickup.
# events_for_pickup 注释），故降为 1：有任何完整文件即复用（--redetect 可强制）
MIN_EVENTS_REUSE = 1

_BY_SLUG = re.compile(r"[/\\\s:|\"'?*]+")


def _slug(s, limit=40):
    return _BY_SLUG.sub("_", s)[:limit].strip("_") or "clip"


def _mmss(t):
    m, s = divmod(int(round(t)), 60)
    return f"{m:02d}m{s:02d}s"


def _part_no(video: Path):
    """文件名里的分片号：`... P3 09日00点04分.mp4` -> 3。"""
    m = re.search(r"P(\d+)", video.stem)
    return m.group(1) if m else "-"


# ---------------------------------------------------------------- 视频收集

def collect_videos(target):
    """目标（目录/文件/登记id）-> 按分片序排序的视频路径列表。"""
    p = Path(target).expanduser()
    if p.is_dir():
        vids = sorted((f.resolve() for f in p.iterdir()
                       if f.suffix.lower() in {".mp4", ".flv", ".mkv", ".mov"}),
                      key=lambda f: (len(f.stem), f.name))   # P2 排在 P10 前
        if not vids:
            raise RuntimeError(f"{p} 下没有视频文件")
        return vids
    if p.is_file():
        return [p.resolve()]
    from toolbox.ingest import recording_path   # 未登记则抛 FileNotFoundError
    return [recording_path(str(target))]


# ---------------------------------------------------------------- 事件获取

def _existing_events(video: Path):
    """在 data/reports/*/events.jsonl 里按 src 找该视频的事件流；没有返回 None。"""
    for rpt in sorted(REPORTS.glob("*/events.jsonl")):
        lines = [l for l in rpt.read_text(encoding="utf-8").splitlines() if l.strip()]
        if not lines:
            continue
        try:
            first = json.loads(lines[0])
        except json.JSONDecodeError:
            continue
        if first.get("src") != str(video):
            continue
        events = [json.loads(l) for l in lines]
        for e in events:
            e.setdefault("src", str(video))
        return rpt, events
    return None


def events_for(video: Path, config, redetect=False, fps=1.0, workers=1,
               progress=None):
    """取某分片的事件流：优先复用，必要时现场检测并写入 <分片名>_full 报告目录。"""
    if not redetect:
        found = _existing_events(video)
        if found:
            rpt, events = found
            if len(events) >= MIN_EVENTS_REUSE:
                print(f"  {video.name[:36]}：复用事件流 {rpt.parent.name}（{len(events)} 条）")
                return events
            print(f"  {video.name[:36]}：已有事件流过稀（{len(events)} 条），重新检测")
    events = scan_video(video, fps=fps, config=config, progress=progress,
                        workers=workers)
    name = _slug(video.stem) + "_full"
    out = REPORTS / name / "events.jsonl"
    out.parent.mkdir(parents=True, exist_ok=True)
    write_jsonl(events, out, src=str(video))
    print(f"  {video.name[:36]}：检测完成 -> {out.parent.name}（{len(events)} 条）")
    return [json.loads(l) for l in out.read_text(encoding="utf-8").splitlines()
            if l.strip()]


# ---------------------------------------------------------------- 裁剪

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
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
                    "-ss", f"{t0:.2f}", "-to", f"{t1:.2f}", "-i", str(src),
                    "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
                    "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "160k",
                    "-movflags", "+faststart", str(out)], check=True)


def knockdowns(target, config=None, pre=None, post=None, merge=None,
               redetect=False, fps=1.0, workers=1):
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
        part = _part_no(video)
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
