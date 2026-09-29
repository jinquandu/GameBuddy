"""游戏报告渲染：从事件池（data/reports/*_full）汇总逐局战绩表（Markdown）。

逐局计算（基准钟/跨零点/局窗口）在 toolbox/chrono.py（2026-09-22 下沉，
口径 2026-09-10 用钢牙整晚 7 局复算验证）；本模块只做 Markdown 渲染。
产出：data/reports/game_report_<月日>.md。

原 `from toolbox.remix import discover_pool` 是死导入（本模块从未调用），
已删除——report 不再在导入期拖入服务端渲染依赖（remix 导入期有字体
探测副作用，曾使 pickups 等无关命令潜在报错）。
"""
from pathlib import Path

from toolbox.chrono import build_matches  # noqa: F401  转引：历史消费方从本模块导入
from toolbox.config import REPORTS


def _clock(abs_sec):
    h, rem = divmod(int(round(abs_sec)), 3600)
    m, s = divmod(rem, 60)
    return f"{h % 24:02d}:{m:02d}:{s:02d}"


def _dur(sec):
    m, s = divmod(int(round(sec)), 60)
    return f"{m}分{s:02d}秒"


def render_report(matches, out_name=None):
    """逐局战绩表（Markdown）落盘 data/reports/，返回 (markdown, path)。"""
    lines = ["| 局 | 进入 | 干员 | 主武器 | 时长 | 击倒 | 多击倒(20s) | 官方击杀 | 拾取 | 撤离 | 本局收获 |",
             "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |"]
    for m in matches:
        sizes = "+".join(str(x) for x in m["multi_sizes"])
        multi = f"{m['multi']}次({sizes})" if m["multi"] else "0"
        ext = {True: "✅", False: "❌"}.get(m["ok"], "-")
        profit = (f"{m['profit']:,}" if m["profit"] is not None
                  else "0（装备损失）" if m["ok"] is False else "-")
        ko = m["kills_official"] if m["kills_official"] is not None else "-"
        op = f"{m['operator']}({m['operator_class']})" if m["operator"] else "-"
        lines.append(f"| {m['no']} | {_clock(m['enter'])} | {op} | "
                     f"{m['primary_weapon'] or '-'} | {_dur(m['dur'])} | "
                     f"{m['downs']} | {multi} | {ko} | {m['loots']} | {ext} | {profit} |")
    t = matches
    lines.append(f"| **合计** | | | | | **{sum(x['downs'] for x in t)}** | "
                 f"**{sum(x['multi'] for x in t)}次** | | "
                 f"**{sum(x['loots'] for x in t)}** | "
                 f"**{sum(1 for x in t if x['ok'])}/{len(t)}** | "
                 f"**{sum(x['profit'] or 0 for x in t):,}** |")
    md = "\n".join(lines)
    out = Path(REPORTS) / (out_name or "game_report.md")
    out.write_text(md + "\n", encoding="utf-8")
    return md, out
