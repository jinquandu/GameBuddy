"""游戏报告：从事件池（data/reports/*_full）汇总逐局战绩，纯汇总零额外识别。

口径（与 events.jsonl 对齐，2026-09-10 用钢牙整晚 7 局复算验证）：
- 进入 = 分片基准钟（文件名「DD日HH点MM分」）+ match_start.t_start；
- 时长 = 本局撤离结算 t_end − 进入（无结算则取下一局进入时刻）；
  对局跨分片（如 P1 打到 P2 才结算）用绝对时间续算；
- 击倒/拾取 = 局窗口内的 down/loot 事件计数；
- 多击倒(20s) = 局内 down 事件按 20s 间隔聚簇，人数 ≥2 的爆发段数
  （如 3 次(2+2+2)：三段爆发，每段连倒 2 人）；
- 官方击杀/本局收获 = 撤离结算快照 OCR（meta.kills_official / meta.profit），
  中文数字转阿拉伯，缺识别记 "-"；撤离失败记 0（装备损失）。

产出：data/reports/game_report_<月日>.md
"""
import json
import re
from pathlib import Path

from toolbox.config import REPORTS
from toolbox.remix import discover_pool

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


def _part_base(src):
    """文件名「…DD日HH点MM分.mp4」-> 当日基准秒；跨零点分片补 24h 保序。"""
    m = re.search(r"(\d{1,2})日(\d{1,2})点(\d{1,2})分", Path(src).name)
    if not m:
        return None
    return int(m.group(2)) * 3600 + int(m.group(3)) * 60


def build_matches(pool):
    """事件池 -> 逐局 dict 列表（绝对时间对齐，支持跨分片对局）。"""
    # 各分片基准钟；跨零点（基准钟回跳）补一天
    bases, prev = {}, -1
    for src in pool["videos"]:
        b = _part_base(src)
        if b is None:
            continue
        if prev >= 0 and b < prev - 12 * 3600:
            b += 86400
        bases[src] = prev = b

    starts = sorted(
        ({"src": e["src"], "t": e["t_start"], "abs": bases[e["src"]] + e["t_start"]}
         for e in pool["events"] if e["kind"] == "match_start" and e["src"] in bases),
        key=lambda x: x["abs"])
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
        matches.append({
            "no": i + 1, "enter": s["abs"],
            "dur": (ext["abs1"] if ext else nxt) - s["abs"],
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


def _clock(abs_sec):
    h, rem = divmod(int(round(abs_sec)), 3600)
    m, s = divmod(rem, 60)
    return f"{h % 24:02d}:{m:02d}:{s:02d}"


def _dur(sec):
    m, s = divmod(int(round(sec)), 60)
    return f"{m}分{s:02d}秒"


def render_report(matches, out_name=None):
    """逐局战绩表（Markdown）落盘 data/reports/，返回 (markdown, path)。"""
    lines = ["| 局 | 进入 | 时长 | 击倒 | 多击倒(20s) | 官方击杀 | 拾取 | 撤离 | 本局收获 |",
             "| --- | --- | --- | --- | --- | --- | --- | --- | --- |"]
    for m in matches:
        sizes = "+".join(str(x) for x in m["multi_sizes"])
        multi = f"{m['multi']}次({sizes})" if m["multi"] else "0"
        ext = {True: "✅", False: "❌"}.get(m["ok"], "-")
        profit = (f"{m['profit']:,}" if m["profit"] is not None
                  else "0（装备损失）" if m["ok"] is False else "-")
        ko = m["kills_official"] if m["kills_official"] is not None else "-"
        lines.append(f"| {m['no']} | {_clock(m['enter'])} | {_dur(m['dur'])} | "
                     f"{m['downs']} | {multi} | {ko} | {m['loots']} | {ext} | {profit} |")
    t = matches
    lines.append(f"| **合计** | | | **{sum(x['downs'] for x in t)}** | "
                 f"**{sum(x['multi'] for x in t)}次** | | "
                 f"**{sum(x['loots'] for x in t)}** | "
                 f"**{sum(1 for x in t if x['ok'])}/{len(t)}** | "
                 f"**{sum(x['profit'] or 0 for x in t):,}** |")
    md = "\n".join(lines)
    out = Path(REPORTS) / (out_name or "game_report.md")
    out.write_text(md + "\n", encoding="utf-8")
    return md, out
