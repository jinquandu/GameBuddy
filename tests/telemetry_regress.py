# -*- coding: utf-8 -*-
"""REQ-20261028-002 方案 A 回归:客户端统计遥测参数 + 日志统计脚本。

- telemetry.query/with_query:参数格式、已有 query 连接、空 URL、非 ASCII 安全;
- rules_urls/manifest_urls 全部候选都带参数;origin 推导与包地址不被污染;
- 不含身份字段(手机号/device_id/token 绝不出现);
- docs/client_stats.py:解析/日聚合/build 分布/unknown 归类/坏行容错/非
  clients 路径过滤。
"""
import importlib.util
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, ".")

from toolbox import auto_update as au
from toolbox import rules_sync as rs
from toolbox import telemetry as tm
from toolbox.version import BUILD, VERSION

ok = 0


def check(cond, what):
    global ok
    assert cond, "FAIL: " + what
    ok += 1
    print("  ✓ " + what)


# ---------------- 参数格式
q = tm.query()
check(f"build={BUILD}" in q and f"v={VERSION}" in q and "os=win" in q,
      "参数:含 build/v/os")
check(q.isascii(), "参数:纯 ASCII(无需转义)")
check(tm.with_query("https://a/x.json") == f"https://a/x.json?{q}",
      "with_query:无 query 用 ?")
check(tm.with_query("https://a/x.json?a=1") == f"https://a/x.json?a=1&{q}",
      "with_query:已有 query 用 &")
check(tm.with_query("") == "" and tm.with_query(None) == "",
      "with_query:空值原样返回")
check("device" not in q.lower() and "token" not in q.lower()
      and "phone" not in q.lower(), "参数:不含身份字段")

# ---------------- 两个轮询全候选都带参数
for name, urls in (("rules", rs.rules_urls()), ("update", au.manifest_urls())):
    check(urls and all(("build=" in u and "os=" in u) for u in urls),
          f"{name}:全部候选地址都带统计参数")
    check(all(("?" in u) and u.count("?") == 1 for u in urls),
          f"{name}:每址仅一个 ?")

# ---------------- 包地址不被污染
pkg = au._url_candidates("/eval/clients/dashijie-pet-x.zip")
check(pkg and all("client-update.json" not in u for u in pkg),
      "包候选:不含清单路径")
check(all("build=" not in u for u in pkg),
      "包候选:不带统计参数(下载大包不需要)")

# ---------------- 统计脚本
sys.path.insert(0, "docs")
spec = importlib.util.spec_from_file_location(
    "client_stats", Path("docs/client_stats.py"))
cs = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cs)

lines = [
    '1.2.3.4 - - [28/Oct/2026:09:00:01 +0800] "GET /eval/clients/client-rules.json'
    '?build=20261028-r16&v=0.1&os=win HTTP/1.1" 200 237 "-" "x"',
    '1.2.3.4 - - [28/Oct/2026:15:00:01 +0800] "GET /eval/clients/client-update.json'
    '?build=20261028-r16&v=0.1&os=win HTTP/1.1" 200 553 "-" "x"',
    '5.6.7.8 - - [28/Oct/2026:10:00:01 +0800] "GET /eval/clients/client-rules.json'
    '?build=20261026-r9c&v=0.1&os=win HTTP/1.1" 200 237 "-" "x"',
    '9.9.9.9 - - [28/Oct/2026:11:00:01 +0800] "GET /eval/clients/client-update.json'
    ' HTTP/1.1" 200 553 "-" "x"',
    '1.2.3.4 - - [27/Oct/2026:09:00:01 +0800] "GET /eval/clients/client-rules.json'
    '?build=20261028-r16&v=0.1&os=win HTTP/1.1" 200 237 "-" "x"',
    '7.7.7.7 - - [28/Oct/2026:12:00:01 +0800] "GET /eval/usr_x/preview/y'
    ' HTTP/1.1" 200 100 "-" "x"',
    'garbage line',
]
per, os_per, total, bad = cs.parse(lines)
check(total == 5, f"脚本:只计 clients 路径(实得 {total})")
check(bad == 1, "脚本:坏行可容错")
rep = cs.report(per, os_per)
daily = {r["date"]: r["active_ips"] for r in rep["daily"]}
check(daily.get("2026-10-28") == 3, f"脚本:28 日唯一 IP=3（实得 {daily}）")
check(daily.get("2026-10-27") == 1, "脚本:27 日唯一 IP=1（跨日分隔）")
builds = {r["build"]: r["active_ips"] for r in rep["builds"]}
check(builds.get("20261028-r16") == 1 and builds.get("20261026-r9c") == 1
      and builds.get("unknown") == 1, f"脚本:build 分布三组（实得 {builds}）")
check(rep["platforms"] and rep["platforms"][0]["os"] == "win",
      "脚本:平台聚合")

with tempfile.TemporaryDirectory() as td:
    log = Path(td) / "c.log"
    log.write_text("\n".join(lines) + "\n", encoding="utf-8")
    out = Path(td) / "r.json"
    rc = cs.main(["--log", str(log), "--json", str(out)])
    check(rc == 0 and out.is_file(), "脚本:CLI 生成 JSON 报表")

print("PASS: " + str(ok) + " 项断言全部通过")
