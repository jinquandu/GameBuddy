"""素材收集与事件池获取（识别链公共入口，原寄生在 knockdown）。

事件流 events.jsonl 是「扫完一次性写出」（write-once）：存在的文件即为
完整结论，跑刀等安静分片全片仅个位数事件是常态。三条获取路径按链路口径
分层（knockdown 复用有下限，pickup 空文件也是结论，见各函数 docstring）。
"""
import json
import re
from pathlib import Path

from toolbox.config import REPORTS
from toolbox.detector import scan_video, write_jsonl
from toolbox.naming import slug

# 事件流已存在即复用（--redetect 强制重扫）；旧阈值 20 会把安静分片误判为
# 未扫完而整段重扫（2026-09-20 本机 3 个分片白扫 ~90min，同 trap 见
# events_for_pickup 注释），故降为 1：有任何完整文件即复用
MIN_EVENTS_REUSE = 1


def part_no(video: Path):
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

def existing_events(video: Path):
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


def events_for(video: Path, config, redetect=False, fps=1.0, workers=None,
               progress=None):
    """取某分片的事件流（knockdown/pipeline 口径）：优先复用，必要时现场
    检测并写入 <分片名>_full 报告目录。复用要求 >= MIN_EVENTS_REUSE 条。
    workers=None 走 scan_video 自动（config detector.workers 或按时长/核数）。"""
    if not redetect:
        found = existing_events(video)
        if found:
            rpt, events = found
            if len(events) >= MIN_EVENTS_REUSE:
                print(f"  {video.name[:36]}：复用事件流 {rpt.parent.name}（{len(events)} 条）")
                return events
            print(f"  {video.name[:36]}：已有事件流过稀（{len(events)} 条），重新检测")
    events = scan_video(video, fps=fps, config=config, progress=progress,
                        workers=workers)
    name = slug(video.stem) + "_full"
    out = REPORTS / name / "events.jsonl"
    out.parent.mkdir(parents=True, exist_ok=True)
    write_jsonl(events, out, src=str(video))
    print(f"  {video.name[:36]}：检测完成 -> {out.parent.name}（{len(events)} 条）")
    return [json.loads(l) for l in out.read_text(encoding="utf-8").splitlines()
            if l.strip()]


def events_for_pickup(video: Path, config, fps=1.0, workers=1):
    """取某分片的事件流（pickup 口径）：已有的该源报告一律复用；没有才现场检测。

    两种复用路径：约定路径 <slug视频名>_full 存在即用——**空文件也是结论**
    （无对局分片此前已付过整段重扫成本，events_for 的 MIN_EVENTS_REUSE
    「过稀重扫」保护在这里只会对其再白扫约 1 小时）；否则按 src 匹配任意
    报告目录（detector 时代旧命名），同样无论多稀都复用。
    """
    conv = REPORTS / (slug(video.stem) + "_full") / "events.jsonl"
    if conv.exists():
        lines = [l for l in conv.read_text(encoding="utf-8").splitlines()
                 if l.strip()]
        print(f"  {video.name[:36]}：复用报告 {conv.parent.name}"
              f"（{len(lines)} 条）", flush=True)
        return [json.loads(l) for l in lines]
    found = existing_events(video)
    if found:
        rpt, events = found
        print(f"  {video.name[:36]}：复用事件流 {rpt.parent.name}"
              f"（{len(events)} 条）", flush=True)
        return events
    return events_for(video, config, fps=fps, workers=workers)
