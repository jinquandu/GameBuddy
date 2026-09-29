"""跨分片基准钟与逐局对齐（全链时间口径的唯一实现）。

录制分片名（recorder 产 `<标题> P<k> DD日HH点MM分.mp4`）是全链时间对齐的
根基：events.jsonl 的分片内相对时间戳 + 该分片基准钟 = 跨分片绝对时间
（pickups.json 的 abs_t，契约见 docs/SERVER-INTERFACE.md §5）。

原实现寄生在 report.py（pickup 经 `from toolbox.report import ...` 反向拖入
服务端渲染链，且 +86400 跨零点补一天逻辑在 report/pickup 各一份），2026-09-22
下沉为中性模块：report 只管 Markdown 渲染，pickup 不再 import report。
"""
import re
from pathlib import Path

_STAMP = re.compile(r"(\d{1,2})日(\d{1,2})点(\d{1,2})分")

_CN_NUM = {"一": 1, "二": 2, "三": 3, "四": 4, "五": 5,
           "六": 6, "七": 7, "八": 8, "九": 9}


def _to_int(s):
    """结算快照里的数字（'三' / '3' / '8,926,620'）转 int；不成返回 None。"""
    s = str(s).strip()
    if s.isdigit():
        return int(s)
    if len(s) == 1 and s in _CN_NUM:
        return _CN_NUM[s]
    plain = s.replace(",", "")
    return int(plain) if plain.isdigit() else None


def part_base(src):
    """文件名「…DD日HH点MM分.mp4」-> 当日基准秒；无时间戳返回 None。"""
    m = _STAMP.search(Path(src).name)
    if not m:
        return None
    return int(m.group(2)) * 3600 + int(m.group(3)) * 60


def bases_for(srcs, on_missing="skip"):
    """各分片基准钟 dict；跨零点（基准钟回跳 >12h）补一天保序。

    on_missing：无时间戳分片（download 产的分 P 名不是时间戳）的处理——
    "skip" 整片跳过（report/build_matches 口径），"zero" 按 0 计（pickup
    算 abs_t 的口径，原 pickup._bases 行为）。
    """
    bases, prev = {}, -1
    for src in srcs:
        b = part_base(src)
        if b is None:
            if on_missing == "skip":
                continue
            b = 0
        if prev >= 0 and b < prev - 12 * 3600:
            b += 86400
        bases[str(src)] = prev = b
    return bases


def build_matches(pool):
    """事件池 -> 逐局 dict 列表（绝对时间对齐，支持跨分片对局）。

    口径（2026-09-10 用钢牙整晚 7 局复算验证）：
    - 进入 = 分片基准钟（文件名「DD日HH点MM分」）+ match_start.t_start；
    - 时长 = 本局撤离结算 t_end − 进入（无结算则取下一局进入时刻）；
    - 击倒/拾取 = 局窗口内的 down/loot 事件计数；
    - 多击倒(20s) = 局内 down 事件按 20s 间隔聚簇，人数 ≥2 的爆发段数；
    - 官方击杀/本局收获 = 撤离结算快照 OCR（meta.kills_official / meta.profit）。
    """
    bases = bases_for(pool["videos"], on_missing="skip")

    starts = sorted(
        ({"src": e["src"], "t": e["t_start"], "abs": bases[e["src"]] + e["t_start"]}
         for e in pool["events"] if e["kind"] == "match_start" and e["src"] in bases),
        key=lambda x: x["abs"])
    # loadout 与 match_start 同 src 同 t_start 产出（detect 聚合后对齐）
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
        # 计数窗口开到下一局进入：结算后大厅整备同样触发容器计数（拾取）
        win = lambda e: s["abs"] <= abs_of(e) < nxt
        downs = [e["t_start"] for e in pool["events"]
                 if e["kind"] == "down" and win(e)]
        bursts = []                                   # 20s 内连倒 ≥2 为一次爆发
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
            "kills_official": (_to_int(ext["meta"].get("kills_official"))
                               if ext and ext["ok"] else None),
            "loots": sum(1 for e in pool["events"] if e["kind"] == "loot" and win(e)),
            "ok": ext["ok"] if ext else None,
            "profit": _to_int(ext["meta"]["profit"])
            if ext and ext["ok"] and ext["meta"].get("profit") else None,
        })
    return matches
