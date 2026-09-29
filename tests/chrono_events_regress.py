# -*- coding: utf-8 -*-
"""步骤 1/2 重构回归：新旧实现输出必须逐字节一致（用本机真实事件池）。"""
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, ".")

# ---------------- 旧实现（重构前 report.py / pickup.py 原样重建） ----------------
_CN_NUM = {"一": 1, "二": 2, "三": 3, "四": 4, "五": 5,
           "六": 6, "七": 7, "八": 8, "九": 9}


def _old_to_int(s):
    s = str(s).strip()
    if s.isdigit():
        return int(s)
    if len(s) == 1 and s in _CN_NUM:
        return _CN_NUM[s]
    plain = s.replace(",", "")
    return int(plain) if plain.isdigit() else None


def _old_part_base(src):
    m = re.search(r"(\d{1,2})日(\d{1,2})点(\d{1,2})分", Path(src).name)
    if not m:
        return None
    return int(m.group(2)) * 3600 + int(m.group(3)) * 60


def _old_build_matches(pool):
    bases, prev = {}, -1
    for src in pool["videos"]:
        b = _old_part_base(src)
        if b is None:
            continue
        if prev >= 0 and b < prev - 12 * 3600:
            b += 86400
        bases[src] = prev = b

    starts = sorted(
        ({"src": e["src"], "t": e["t_start"], "abs": bases[e["src"]] + e["t_start"]}
         for e in pool["events"] if e["kind"] == "match_start" and e["src"] in bases),
        key=lambda x: x["abs"])
    loadouts = {(e["src"], e["t_start"]): e.get("meta", {})
                for e in pool["events"] if e["kind"] == "loadout"}
    extracts = sorted(
        ({"src": e["src"], "t0": e["t_start"], "t1": e["t_end"],
          "abs1": bases[e["src"]] + e["t_end"], "ok": "成功" in e["detail"],
          "meta": e.get("meta", {})}
         for e in pool["events"] if e["kind"] == "extract" and e["src"] in bases),
        key=lambda x: x["abs1"])

    def abs_of(e):
        return bases[e["src"]] + e["t_start"]

    matches = []
    for i, s in enumerate(starts):
        nxt = starts[i + 1]["abs"] if i + 1 < len(starts) else 1e18
        ext = next((x for x in extracts if s["abs"] <= x["abs1"] < nxt), None)
        win = lambda e: s["abs"] <= abs_of(e) < nxt
        downs = [e["t_start"] for e in pool["events"]
                 if e["kind"] == "down" and win(e)]
        bursts = []
        for t in sorted(downs):
            if bursts and t - bursts[-1][-1] <= 20:
                bursts[-1].append(t)
            else:
                bursts.append([t])
        bursts = [b for b in bursts if len(b) >= 2]
        ld = loadouts.get((s["src"], s["t"]), {})
        matches.append({
            "no": i + 1, "enter": s["abs"],
            "dur": (ext["abs1"] if ext else nxt) - s["abs"],
            "operator": ld.get("operator", ""),
            "operator_class": ld.get("operator_class", ""),
            "primary_weapon": ld.get("primary_weapon", ""),
            "downs": len(downs),
            "multi": len(bursts), "multi_sizes": [len(b) for b in bursts],
            "kills_official": (_old_to_int(ext["meta"].get("kills_official"))
                               if ext and ext["ok"] else None),
            "loots": sum(1 for e in pool["events"] if e["kind"] == "loot" and win(e)),
            "ok": ext["ok"] if ext else None,
            "profit": _old_to_int(ext["meta"]["profit"])
            if ext and ext["ok"] and ext["meta"].get("profit") else None,
        })
    return matches


def _old_bases(videos):
    bases, prev = {}, -1
    for v in videos:
        b = _old_part_base(str(v))
        if b is None:
            b = 0
        if prev >= 0 and b < prev - 12 * 3600:
            b += 86400
        bases[str(v)] = prev = b
    return bases


_BY_SLUG = re.compile(r"[/\\\s:|\"'?*]+")


def _old_slug(s, limit=40):
    return _BY_SLUG.sub("_", s)[:limit].strip("_") or "clip"


def _old_mmss(t):
    m, s = divmod(int(round(t)), 60)
    return f"{m:02d}m{s:02d}s"


# ---------------- 新实现 ----------------
from toolbox import chrono, events, naming

# ---------------- 用真实事件池对比 ----------------
from toolbox.config import REPORTS

pool = {"videos": [], "events": []}
for rpt in sorted(REPORTS.glob("*_full/events.jsonl")):
    lines = [l for l in rpt.read_text(encoding="utf-8").splitlines() if l.strip()]
    if not lines:
        continue
    evs = [json.loads(l) for l in lines]
    src = evs[0].get("src")
    if src and src not in pool["videos"]:
        pool["videos"].append(src)
    pool["events"] += evs
print(f"事件池：{len(pool['videos'])} 分片，{len(pool['events'])} 事件")

old_m = _old_build_matches(pool)
new_m = chrono.build_matches(pool)
assert json.dumps(old_m, ensure_ascii=False) == json.dumps(new_m, ensure_ascii=False), \
    "build_matches 新旧输出不一致！"
print(f"build_matches 一致：{len(new_m)} 局 ✓")

vs = [Path(s) for s in pool["videos"]]
assert _old_bases(vs) == chrono.bases_for(vs, on_missing="zero"), "_bases(zero) 不一致"
print(f"bases_for(zero) 一致：{len(vs)} 分片 ✓")

# skip 口径：含混合无时间戳源
mixed = ["a P1 21日23点50分.mp4", "b 无时间戳.mp4", "c P2 22日00点20分.mp4"]
old_skip = {}
prev = -1
for src in mixed:                       # 旧 report 口径
    b = _old_part_base(src)
    if b is None:
        continue
    if prev >= 0 and b < prev - 12 * 3600:
        b += 86400
    old_skip[src] = prev = b
assert old_skip == chrono.bases_for(mixed, on_missing="skip"), "bases_for(skip) 不一致"
# zero 口径与旧 _bases 全对齐（无时间戳分片同样参与跨零点补一天）
assert _old_bases([Path(m) for m in mixed]) == chrono.bases_for(mixed, on_missing="zero"), \
    "bases_for(zero) 混合源不一致"
print("bases_for skip/zero 双口径 ✓（跨零点 +86400：",
      chrono.bases_for(mixed, on_missing="skip")["c P2 22日00点20分.mp4"], "）")

# naming 与旧实现一致性（含原样保留的 asr 私有 slug 不受影响）
for s in ["猪猪夏 三角洲 07/15", '带"引号"|冒号', "", "  ", "正常标题"]:
    assert _old_slug(s) == naming.slug(s), f"slug 不一致: {s!r}"
    assert _old_slug(s, 10) == naming.slug(s, 10)
for t in (0, 59.6, 61, 3661.4, -0.4):
    assert _old_mmss(t) == naming.mmss(t), f"mmss 不一致: {t}"
print("naming slug/mmss 与旧实现一致 ✓")

# events 复用路径（不触发检测）：真实分片经 events_for 应命中复用
v0 = Path(pool["videos"][0])
got = events.events_for(v0, {})
print(f"events_for 复用路径 ✓（{v0.name[:30]} -> {len(got)} 条）")
import toolbox.pickup as pk
assert pk.events_for_pickup is events.events_for_pickup
print("pickup.events_for_pickup 兼容转引 ✓")
print("\nALL REGRESSION CHECKS PASSED")
