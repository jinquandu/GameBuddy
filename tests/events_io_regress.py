"""events.jsonl 读写回归（P0-4，2026-10）：原子写不留 .tmp；尾行损坏
（= 写入中途被杀）不复用、走重扫自愈；中间坏行跳过不炸。

不依赖 OCR：REPORTS 指到临时目录，scan_video 注入桩。

跑法：py -3 tests/events_io_regress.py
"""
import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import toolbox.events as E  # noqa: E402
from toolbox.detector import DetectedEvent, write_jsonl  # noqa: E402
from toolbox.naming import slug  # noqa: E402

FAIL = []


def check(name, cond, extra=""):
    print(("PASS " if cond else "FAIL ") + name + (f" {extra}" if extra else ""))
    if not cond:
        FAIL.append(name)


def ev(i):
    return DetectedEvent(kind="down", t_start=float(i), t_end=float(i) + 1,
                         detail=f"击倒 目标{i}", confidence=0.9)


tmp = Path(tempfile.mkdtemp(prefix="events_io_"))
rpt = tmp / "demo_full"
rpt.mkdir()
video = tmp / "标题 P1 01日02点03分.mp4"
E.REPORTS = tmp          # 指到临时目录（existing_events/复用/落盘全走这里）

# ---- 1) 原子写：内容完整 + 无 .tmp 残留 ----
write_jsonl([ev(1), ev(2)], rpt / "events.jsonl", src=str(video))
lines = (rpt / "events.jsonl").read_text(encoding="utf-8").splitlines()
check("原子写内容完整", len(lines) == 2
      and json.loads(lines[0])["detail"] == "击倒 目标1")
check("无 tmp 残留", not list(rpt.glob("*.tmp")))

# ---- 2) 好文件复用：existing_events 命中 ----
found = E.existing_events(video)
check("好文件复用", bool(found) and found[0] == rpt / "events.jsonl"
      and len(found[1]) == 2)

# ---- 3) 尾行损坏：不复用（返回 None → 上层走重扫） ----
# 用独立 video3：不能让 src 同源的好报告（demo_full）合理抢先复用
video3 = tmp / "标题C P1 01日02点03分.mp4"
bad_tail = tmp / "badtail_full" / "events.jsonl"
bad_tail.parent.mkdir()
good3 = DetectedEvent(kind="down", t_start=1, t_end=2,
                      detail="x", confidence=0.9).to_line(str(video3))
bad_tail.write_text(good3 + "\n" + '{"kind": "down", "t_s', encoding="utf-8")
check("尾行损坏不复用", E.existing_events(video3) is None)

# ---- 4) 中间坏行：跳过该行，其余复用 ----
mid = tmp / "midbad_full" / "events.jsonl"
mid.parent.mkdir()
mid.write_text("\n".join([good3, '{"broken', good3]) + "\n",
               encoding="utf-8")
# 解析层单独验证（不走 src 匹配）
events = E._read_events(mid.read_text(encoding="utf-8").splitlines(), "mid")
check("中间坏行跳过", events is not None and len(events) == 2)

# ---- 5) 自愈：损坏的规范名报告 → events_for 走重扫并原子重写 ----
# 用独立 video2：既有好报告（src=video）不能经 existing_events 抢先复用
video2 = tmp / "标题B P1 01日02点03分.mp4"
conv = tmp / (slug(video2.stem) + "_full") / "events.jsonl"
conv.parent.mkdir(parents=True, exist_ok=True)
good2 = DetectedEvent(kind="down", t_start=1, t_end=2,
                      detail="x", confidence=0.9).to_line(str(video2))
conv.write_text(good2 + "\n" + '{"kind": "loo', encoding="utf-8")  # 截断尾
rescanned = []
E.scan_video = lambda *a, **k: rescanned.append(1) or [ev(9)]   # 桩：记录调用
out = E.events_for_pickup(video2, config=None)
check("截断触发重扫", len(rescanned) == 1)
check("重扫结果返回", len(out) == 1 and out[0]["detail"] == "击倒 目标9")
healed = conv.read_text(encoding="utf-8").splitlines()
check("损坏文件被原子重写", len(healed) == 1
      and json.loads(healed[0])["detail"] == "击倒 目标9")
check("重写后无 tmp 残留", not list(conv.parent.glob("*.tmp")))

# ---- 6) 空报告 = 0 事件结论：三条获取路径一律复用，不重扫 ----
# 2026-10-15 exe 实测 P1-1：0 事件分片曾因 knockdown 口径不认空文件，
# 在该阶段整段重扫 474 秒（pickups 却秒级复用同一份报告）
video4 = tmp / "标题D P2 04日05点06分.mp4"
conv4 = tmp / (slug(video4.stem) + "_full") / "events.jsonl"
conv4.parent.mkdir(parents=True, exist_ok=True)
conv4.write_text("", encoding="utf-8")             # detect 扫完写出 0 条
found4 = E.existing_events(video4)
check("空规范报告复用(existing_events)", bool(found4)
      and found4[0] == conv4.parent and found4[1] == [])
rescanned.clear()
out4 = E.events_for(video4, config=None)           # knockdown/pipeline 口径
check("空规范报告复用(events_for 不重扫)",
      out4 == [] and not rescanned)
out4p = E.events_for_pickup(video4, config=None)   # pickup 口径
check("空规范报告复用(events_for_pickup)",
      out4p == [] and not rescanned)

# ---- 7) 无任何报告：仍走现场检测（空文件≠不存在） ----
video5 = tmp / "标题E P1 01日02点03分.mp4"
rescanned.clear()
E.events_for(video5, config=None)
check("无报告才现场检测", len(rescanned) == 1)

print("\n" + ("全部通过 ✅" if not FAIL else f"失败 {len(FAIL)} 项 ❌：{FAIL}"))
sys.exit(1 if FAIL else 0)
