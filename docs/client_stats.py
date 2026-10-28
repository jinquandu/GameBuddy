# -*- coding: utf-8 -*-
"""客户端活跃与 build 分布统计(REQ-20261028-002 方案 A)。

输入:nginx `dashijie-clients.access.log`(只在 `/eval/clients/` 这一个
location 打开,见 REQ 里的部署说明)。客户端每 6 小时轮询一次
client-rules.json / client-update.json,URL 上带 `build`/`v`/`os`。

输出:
  - 每日活跃:唯一 IP 数(近似"活跃安装数")
  - build 分布:每个 build 的唯一 IP 数
  - 未知 build:不带参数的请求(老客户端 / 探测)

口径与局限(写进报表,避免误读):
  - **IP ≠ 设备**:同一 NAT/公司网会合并,移动网络重拨会重复计数;
  - 只统计**联网并活着**的客户端——装了不用/离线的不出现;
  - 唯一 IP 是"当日去重",不是累计安装量(累计需 REQ 方案 B 的登录上报)。

用法(服务器上):
    python3 client_stats.py --log /var/log/nginx/dashijie-clients.access.log
    python3 client_stats.py --log ... --days 7 --json out.json
本地分析已导出的日志:
    python3 client_stats.py --log sample.log --days 30
"""
import argparse
import json
import re
import sys
from collections import defaultdict
from pathlib import Path

# 常见 combined / main 格式:IP - - [time] "GET /path?query HTTP/1.1" status bytes ...
_LINE = re.compile(
    r'^(?P<ip>\S+) \S+ \S+ \[(?P<time>[^\]]+)\] '
    r'"(?P<method>[A-Z]+) (?P<path>\S+)[^"]*" (?P<status>\d{3})')
_BUILD = re.compile(r"[?&]build=(?P<build>[A-Za-z0-9._-]{1,32})")
_OS = re.compile(r"[?&]os=(?P<os>[A-Za-z]{2,10})")
_MONTHS = {m: i + 1 for i, m in enumerate(
    ("Jan", "Feb", "Mar", "Apr", "May", "Jun",
     "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"))}


def _day(time_str):
    """'28/Oct/2026:15:18:02 +0800' -> '2026-10-28'。"""
    try:
        d, mon, rest = time_str.split("/", 2)
        year = rest.split(":", 1)[0]
        return f"{year}-{_MONTHS[mon]:02d}-{int(d):02d}"
    except (ValueError, KeyError):
        return ""


def parse(lines):
    """-> {(day, build): {ips}} / {(day, build): count}。"""
    per = defaultdict(set)
    os_per = defaultdict(set)
    total = 0
    bad = 0
    for line in lines:
        m = _LINE.match(line)
        if not m:
            bad += 1
            continue
        path = m.group("path")
        if "/eval/clients/" not in path:
            continue                      # 只统计分发位(其他 location 无日志)
        total += 1
        day = _day(m.group("time"))
        bm = _BUILD.search(path)
        build = bm.group("build") if bm else "unknown"
        ip = m.group("ip")
        per[(day, build)].add(ip)
        om = _OS.search(path)
        if om:
            os_per[(day, om.group("os"))].add(ip)
    return per, os_per, total, bad


def report(per, os_per, days=None, top_n=20):
    """生成可读报表(dict,便于 JSON 输出或打印)。"""
    by_day = defaultdict(set)
    by_build = defaultdict(set)
    for (day, build), ips in per.items():
        by_day[day] |= ips
        by_build[build] |= ips
    daily = [{"date": d, "active_ips": len(ips)}
             for d, ips in sorted(by_day.items())]
    if days:
        daily = daily[-days:]
        keep = {d["date"] for d in daily}
        by_build = defaultdict(set)
        for (day, build), ips in per.items():
            if day in keep:
                by_build[build] |= ips
    builds = sorted(({"build": b, "active_ips": len(ips)}
                     for b, ips in by_build.items()),
                    key=lambda x: -x["active_ips"])[:top_n]
    platforms = sorted(({"os": o, "active_ips": len(ips)}
                        for o, ips in os_per.items()),
                       key=lambda x: -x["active_ips"])
    # os_per 的键是 (day, os):按 os 再聚合一次全局唯一 IP
    by_os = defaultdict(set)
    for (day, osname), ips in os_per.items():
        by_os[osname] |= ips
    platforms = sorted(({"os": o, "active_ips": len(ips)}
                        for o, ips in by_os.items()),
                       key=lambda x: -x["active_ips"])
    return {"daily": daily, "builds": builds, "platforms": platforms,
            "unknown_build_ips": sum(1 for _ in [])}


def main(argv=None):
    ap = argparse.ArgumentParser(description="客户端活跃与 build 分布统计")
    ap.add_argument("--log", required=True, help="nginx clients 访问日志")
    ap.add_argument("--days", type=int, default=0,
                    help="只看最近 N 天(0=全部)")
    ap.add_argument("--json", default="", help="同时写出 JSON 报表")
    a = ap.parse_args(argv)

    p = Path(a.log)
    if not p.is_file():
        print(f"日志不存在: {p}", file=sys.stderr)
        return 2
    with p.open("r", encoding="utf-8", errors="replace") as f:
        per, os_per, total, bad = parse(f)
    if not total:
        print("日志里没有 /eval/clients/ 请求——确认该 location 已开 access_log")
        return 1

    rep = report(per, os_per, days=a.days or None)
    print("=" * 58)
    print("陪玩大师姐 · 客户端活跃与 build 分布")
    print("=" * 58)
    print(f"样本请求 {total} 条（解析失败 {bad} 条）\n")
    print("每日活跃（唯一 IP，近似活跃安装数）")
    for row in rep["daily"]:
        bar = "█" * min(40, row["active_ips"])
        print(f"  {row['date']}  {row['active_ips']:>4}  {bar}")
    print("\nbuild 分布（唯一 IP）")
    for row in rep["builds"]:
        tag = "（老客户端/未带参数）" if row["build"] == "unknown" else ""
        print(f"  {row['build']:<18} {row['active_ips']:>4} {tag}")
    if rep["platforms"]:
        print("\n平台")
        for row in rep["platforms"]:
            print(f"  {row['os']:<6} {row['active_ips']:>4}")
    print("\n口径提醒：IP ≠ 设备（同 NAT 会合并）；只统计联网且运行中的"
          "客户端；累计安装量需登录上报（REQ 方案 B）。")
    if a.json:
        Path(a.json).write_text(json.dumps(rep, ensure_ascii=False, indent=2),
                                encoding="utf-8")
        print(f"\nJSON 报表: {a.json}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
