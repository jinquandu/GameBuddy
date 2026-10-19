"""素材收集与事件池获取（识别链公共入口，原寄生在 knockdown）。

事件流 events.jsonl 是「扫完一次性写出」（write-once 原子写）：**文件存在
即为完整结论，空文件 = 0 事件**（跑刀等安静分片全片仅个位数事件是常态；
2026-10-15 exe 实测曾因 knockdown 口径不认空文件，0 事件分片整段重扫
474 秒）。三条获取路径口径已统一（见各函数 docstring）。
"""
import json
import re
from pathlib import Path

# collect_videos 单一实现已收敛 toolbox/common.py（REQ-20261014-004，双端
# transfer 同源导入）；此处转引保 knockdown/pickup/queues/pipeline/asr
# 历史导入方
from toolbox.common import collect_videos
from toolbox.config import REPORTS
from toolbox.detector import scan_video, write_jsonl
from toolbox.naming import slug


def part_no(video: Path):
    """文件名里的分片号：`... P3 09日00点04分.mp4` -> 3。"""
    m = re.search(r"P(\d+)", video.stem)
    return m.group(1) if m else "-"


# ---------------------------------------------------------------- 事件获取

def _read_events(lines, where):
    """逐行解析事件流，容忍损坏行（历史非原子写留下的截断文件）：

    - **尾行损坏 = 写入中途被杀**，该文件不是完整结论，返回 None 让调用方
      走重扫（否则 knockdowns/pickups 每次启动都炸在同一行，崩溃循环）；
    - 中间孤立坏行（磁盘位损）跳过并告警，其余行照常复用。
    """
    events = []
    for i, l in enumerate(lines):
        try:
            events.append(json.loads(l))
        except json.JSONDecodeError:
            if i == len(lines) - 1:
                print(f"  警告：{where} 尾行损坏（写入中断），不复用、将重扫")
                return None
            print(f"  警告：{where} 第 {i + 1} 行损坏，跳过该行")
    return events


def existing_events(video: Path):
    """在 data/reports 里找该视频的事件流；没有返回 None。

    与 events_for_pickup 同口径：规范目录 <分片stem>_full 优先，**空文件
    也是结论**（原子写后文件存在即完整，0 事件是无对局分片的正常结论；
    src 匹配法对空文件无从归属，只能靠规范目录名）；规范路径尾行损坏
    （写入中断的旧非原子文件）则落回 src 匹配，按首行找任意报告目录
    （detector 时代旧命名，可能只含小扫描子集）。
    """
    conv = REPORTS / (slug(video.stem) + "_full") / "events.jsonl"
    if conv.is_file():
        lines = [l for l in conv.read_text(encoding="utf-8").splitlines()
                 if l.strip()]
        events = _read_events(lines, conv.parent.name)
        if events is not None:
            for e in events:
                e.setdefault("src", str(video))
            return conv.parent, events
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
        events = _read_events(lines, rpt.parent.name)
        if events is None:
            continue
        for e in events:
            e.setdefault("src", str(video))
        return rpt, events
    return None


def events_for(video: Path, config, redetect=False, fps=1.0, workers=None,
               progress=None):
    """取某分片的事件流（knockdown/pipeline 口径）：已有报告一律复用——
    无论多少条，0 条（空文件）也是结论（曾有 MIN_EVENTS_REUSE「过稀重扫」
    保护，2026-10-15 实测让 0 事件分片在 knockdowns 白扫 474 秒后移除；
    完整性由原子写 + 尾行损坏检测保证，与条数无关）。--redetect 或无报告
    才现场检测并写入 <分片名>_full 报告目录。workers=None 走 scan_video
    自动（config detector.workers 或按时长/核数）。
    """
    if not redetect:
        found = existing_events(video)
        if found:
            rpt, events = found
            print(f"  {video.name[:36]}：复用事件流 {rpt.parent.name}（{len(events)} 条）")
            return events
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
    """取某分片的事件流（pickup 口径）：与 events_for 同口径（规范路径优先、
    空文件也是结论）。「空文件也是结论」2026-10-15 前只在本函数成立（同数据
    两条路径行为分裂：pickups 秒级复用、knockdowns 整段重扫 474 秒），当日
    回灌 events_for/existing_events 后合一。保留独立入口与 flush 打印——
    pet GUI 逐行读子进程输出，复用消息实时可见。
    """
    found = existing_events(video)
    if found:
        rpt, events = found
        print(f"  {video.name[:36]}：复用报告 {rpt.parent.name}"
              f"（{len(events)} 条）", flush=True)
        return events
    return events_for(video, config, fps=fps, workers=workers)
