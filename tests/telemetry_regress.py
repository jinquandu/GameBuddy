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
lines_beat = [
    '1.2.3.4 - - [28/Oct/2026:09:00:01 +0800] "GET /eval/clients/beat.json'
    '?build=20261028-r17&v=0.1&os=win&install=AAAABBBBCCCC1111 HTTP/1.1" 200 77 "-" "x"',
    '1.2.3.4 - - [28/Oct/2026:15:00:01 +0800] "GET /eval/clients/beat.json'
    '?build=20261028-r17&v=0.1&os=win&install=AAAABBBBCCCC1111 HTTP/1.1" 200 77 "-" "x"',
    '5.6.7.8 - - [28/Oct/2026:10:00:01 +0800] "GET /eval/clients/beat.json'
    '?build=20261026-r9c&v=0.1&os=win&install=DDDDEEEEFFFF2222 HTTP/1.1" 200 77 "-" "x"',
]
per_b, os_b, inst_b, tot_b, bad_b = cs.parse(lines_beat)
rep_b = cs.report(per_b, os_b, inst_b)
check(tot_b == 3, "信标:三行解析")
check(rep_b["installs"]["total_unique"] == 2,
      "信标:install_id 去重 → 2 台(同机重复上报不重复计)")
bb = {r["build"]: r["installs"] for r in rep_b["installs"]["by_build"]}
check(bb.get("20261028-r17") == 1 and bb.get("20261026-r9c") == 1,
      f"信标:按 build 的安装数({bb})")
check(rep_b["installs"]["daily"][0]["installs"] == 2, "信标:按日安装数")

i1 = tm.install_id()
check(len(i1) == 32 and i1.isalnum(), "install_id:32 位十六进制")
check(tm.install_id() == i1, "install_id:进程内稳定")
import tempfile as _tf
from pathlib import Path as _P
import toolbox.config as _cfg
with _tf.TemporaryDirectory() as td:
    real_data = _cfg.DATA
    real_cached = tm._install_cache["id"]
    _cfg.DATA = _P(td)
    tm._install_cache["id"] = None
    try:
        a = tm.install_id()
        f = _P(td) / ".device" / "install_id"
        check(f.is_file() and f.read_text(encoding="utf-8").strip() == a,
              "install_id:首启落盘")
        tm._install_cache["id"] = None          # 模拟重启
        check(tm.install_id() == a, "install_id:重启复用同一值")
    finally:
        _cfg.DATA = real_data
        tm._install_cache["id"] = real_cached

seen = []
def _fake_open(url, timeout):
    seen.append(url)
    class _R:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def read(self, n=-1): return b"x"
    return _R()
check(tm.send_beat(_open=_fake_open) is True, "信标:发送成功")
check(bool(seen) and "install=" in seen[0] and "build=" in seen[0]
      and "os=" in seen[0], "信标:URL 带 install/build/os")


def _boom(url, timeout):
    raise OSError("net down")
check(tm.send_beat(_open=_boom) is False, "信标:网络失败静默返回 False")

per, os_per, inst, total, bad = cs.parse(lines)
check(total == 5, f"脚本:只计 clients 路径(实得 {total})")
check(bad == 1, "脚本:坏行可容错")
rep = cs.report(per, os_per, inst)
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
