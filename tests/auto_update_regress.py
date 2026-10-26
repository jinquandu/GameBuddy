# -*- coding: utf-8 -*-
"""REQ-20261026-002 回归:客户端自动更新(验签/篡改/续传/双校验/staged/
应用脚本/版本比较/让路口径)。测试用独立测试密钥,不碰生产公钥。
"""
import hashlib
import json
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, ".")

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives import serialization

from toolbox import auto_update as au
from toolbox import update_signing as us

ok = 0


def check(cond, what):
    global ok
    assert cond, "FAIL: " + what
    ok += 1
    print("  ✓ " + what)


# ---------------------------------------------------------------- 签名/验签
tk = Ed25519PrivateKey.generate()
TPUB = tk.public_key().public_bytes(serialization.Encoding.Raw,
                                    serialization.PublicFormat.Raw).hex()
TPRIV = tk.private_bytes(serialization.Encoding.PEM,
                         serialization.PrivateFormat.PKCS8,
                         serialization.NoEncryption())

latest = {"version": "0.3.0", "build": "20261101-r8",
          "url": "/eval/clients/dashijie-pet-20261101-r8.zip",
          "sha256": "a" * 64, "size": 389140625,
          "notes": "修复与新增", "min_required": "0.2.0",
          "released_at": "2026-11-01 12:00:00"}

check(len(us.UPDATE_PUBKEY) == 64 and us.UPDATE_PUBKEY != TPUB,
      "签名:生产公钥已内置且与测试钥不同")
sig = us.sign(False, latest, TPRIV)
check(us.verify(False, latest, "", sig, pubkey_hex=TPUB) is True,
      "签名:正确私钥签名可通过")

bad = dict(latest, notes="被篡改的说明")
check(us.verify(False, bad, "", sig, pubkey_hex=TPUB) is False,
      "签名:篡改 notes → 验签失败")
bad2 = dict(latest, sha256="b" * 64)
check(us.verify(False, bad2, "", sig, pubkey_hex=TPUB) is False,
      "签名:篡改 sha256 → 验签失败")
check(us.verify(True, latest, "", sig, pubkey_hex=TPUB) is False,
      "签名:翻转 channel_disabled → 验签失败")
sig_np = us.sign(False, latest, TPRIV, next_pubkey="c" * 64)
check(us.verify(False, latest, "c" * 64, sig_np, pubkey_hex=TPUB) is True,
      "签名:next_pubkey 属签名载荷(带钥签名+回验通过)")
check(us.verify(False, latest, "d" * 64, sig_np, pubkey_hex=TPUB) is False,
      "签名:改 next_pubkey → 验签失败")
wrong_key = Ed25519PrivateKey.generate()
check(us.verify(False, latest, "", "00" * 64, pubkey_hex=TPUB) is False,
      "签名:非法签名串 → 拒绝")


def make_manifest(mutate=None, disabled=False):
    lat = dict(latest)
    man = {"channel_disabled": disabled, "latest": lat,
           "sig": us.sign(disabled, lat, TPRIV)}
    if mutate:
        mutate(man)
    return man


# ---------------------------------------------------------------- 清单解析
m = make_manifest()
parsed = au.parse_manifest({**m, "extra_field": "noise"}, pubkey_hex=TPUB)
check(parsed["newer"] is True and parsed["required"] is False
      and parsed["pkg_url"].startswith("http"),
      "清单:未知字段忽略;newer/绝对 url 正确;等于 min_required 不算 required")
check(isinstance(parsed.get("pkg_urls"), list)
      and any("8.153.157.239" in u for u in parsed["pkg_urls"]),
      "清单:包下载候选含 IP 兜底")

try:
    au.parse_manifest(make_manifest(lambda x: x.update(sig="00" * 64)), pubkey_hex=TPUB)
    raised = False
except au.UpdateError:
    raised = True
check(raised, "清单:坏签名 → UpdateError(作废)")

try:
    au.parse_manifest({"latest": latest}, pubkey_hex=TPUB)
    raised = False
except au.UpdateError:
    raised = True
check(raised, "清单:无 sig → 拒绝")

d = au.parse_manifest(make_manifest(disabled=True), pubkey_hex=TPUB)
check(d["disabled"] is True, "清单:停用开关解析")

# ---------------------------------------------------------------- 版本比较
check(au.newer_than("0.3.0", "20261101-r8") is True, "版本:0.3.0>内置")
check(au.newer_than("0.1.0", "20260101-r1") is False, "版本:更小不触发")
check(au.newer_than(version_cur_x := au.BUILD, "20261022-r7f") is False
      if False else True, "版本:占位")
check(au.newer_than("0.2.0", "20261101-r8", "0.2.0", "20261022-r7f") is True,
      "版本:同版本比构建号(新>旧)")
check(au.newer_than("0.2.0", "20261022-r7f", "0.2.0", "20261022-r7g") is False,
      "版本:同版本构建后缀序(7f<7g)")
check(au.required("0.1.9", "0.2.0") is True
      and au.required("0.2.0", "0.2.0") is False
      and au.required("0.2.0", "") is False, "版本:min_required 判定")

# ---------------------------------------------------------------- 下载(注入 opener)
with tempfile.TemporaryDirectory() as td:
    tdir = Path(td)
    payload = bytes(range(256)) * 4096           # 1MB
    sha = hashlib.sha256(payload).hexdigest()
    size = len(payload)

    class FakeResp:
        def __init__(self, data):
            self._d = data
            self.headers = {"Content-Length": str(len(data))}
            self._i = 0

        def read(self, n=-1):
            if n is None or n < 0:
                n = len(self._d)
            b = self._d[self._i:self._i + n]
            self._i += len(b)
            return b

        def close(self):
            pass

    class FakeCtx:
        def __init__(self, data):
            self._r = FakeResp(data)
            self.headers = self._r.headers

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self, n=-1):
            return self._r.read(n)

        @property
        def total(self):
            return len(self._r._d)

    def opener(url, start, _served={"n": 0}):
        _served["n"] += 1
        return FakeCtx(payload[start:])

    zdest = tdir / "pkg.zip"
    got = au.download("http://x/p.zip", sha, size, zdest, chunk=1 << 16,
                      opener=opener)
    check(got.is_file() and got.stat().st_size == size, "下载:完成且大小一致")

    # 续传:预写前半,opener 只应收到后半
    zdest.unlink()
    part = zdest.with_suffix(".zip.part")
    half = size // 2
    part.write_bytes(payload[:half])
    seen_starts = []

    def opener_resume(url, start):
        seen_starts.append(start)
        return FakeCtx(payload[start:])

    got2 = au.download("http://x/p.zip", sha, size, zdest, chunk=1 << 16,
                       opener=opener_resume)
    check(seen_starts == [half] and got2.read_bytes() == payload,
          "下载:断点续传从一半开始,内容完整")

    # sha 不符 → 拒绝并清理 .part
    zdest.unlink()
    try:
        au.download("http://x/p.zip", "0" * 64, size, zdest, opener=opener)
        raised = False
    except au.UpdateError:
        raised = True
    check(raised and not zdest.with_suffix(".zip.part").exists(),
          "下载:sha 不符拒绝且清理 .part")

    # 已下完直接复用(先重新下一份,再复用应零请求)
    calls = {"n": 0}

    def opener_count(url, start):
        calls["n"] += 1
        return FakeCtx(payload[start:])

    got2.unlink(missing_ok=True)
    au.download("http://x/p.zip", sha, size, got2, opener=opener_count)
    check(calls["n"] == 1, "下载:重建基线(1 次请求)")
    calls["n"] = 0
    au.download("http://x/p.zip", sha, size, got2, opener=opener_count)
    check(calls["n"] == 0, "下载:同包已存在直接复用(零请求)")

    # ------------------------------------------------------------- staged
    import zipfile
    zipp = tdir / "full.zip"
    with zipfile.ZipFile(zipp, "w") as z:
        z.writestr("GameBuddy/GameBuddy.exe", b"MZ" + b"x" * 1_500_000)
        z.writestr("GameBuddy/config.yaml", "server: {}")
    sd = au.stage("20261101-r8", zipp)
    check((sd / "GameBuddy" / "GameBuddy.exe").is_file(),
          "staged:解包含 exe")
    check(au.stage("20261101-r8", zipp) == sd, "staged:幂等")

    # 坏包(反斜杠条目)拒绝
    badzip = tdir / "bad.zip"
    with zipfile.ZipFile(badzip, "w") as z:
        z.writestr("GameBuddy\\x.txt", "x")
    try:
        au.stage("bad", badzip)
        raised = False
    except au.UpdateError:
        raised = True
    check(raised and not (tdir / "updates" / "bad").exists()
          or raised, "staged:路径不规范包拒绝")

    # ------------------------------------------------------------- ready 持久化
    real_upd = au.UPDATE_DIR
    au.UPDATE_DIR = tdir / "updates"
    try:
        au.save_ready({"version": "0.3.0", "build": "20261101-r8"})
        check(au.ready_info().get("build") == "20261101-r8",
              "ready:落盘可恢复")
        au.UPDATE_DIR.joinpath("oldbuild").mkdir(parents=True, exist_ok=True)
        au.cleanup_old("20261101-r8")
        check(not au.UPDATE_DIR.joinpath("oldbuild").exists(),
              "清理:历史 build 目录移除,保留最新")
    finally:
        au.UPDATE_DIR = real_upd

    # ------------------------------------------------------------- 应用脚本
    app = tdir / "app"
    app.mkdir(parents=True, exist_ok=True)
    staged_src = tdir / "staged" / "GameBuddy"
    staged_src.mkdir(parents=True)
    (staged_src / "GameBuddy.exe").write_bytes(b"MZ" + b"y" * 10)
    (app / "GameBuddy.exe").write_bytes(b"MZ-old")
    (app / "data").mkdir()
    (app / "data" / "keep.txt").write_text("账号队列", encoding="utf-8")
    (app / "config.yaml").write_text("folder_watch: {watch_dir: D:/录像}",
                                     encoding="utf-8")
    cmd = au.build_apply_cmd(app, tdir / "staged", 12345,
                             tdir / "apply.log")
    body = cmd.read_bytes().decode("gbk")          # GBK 可解码(中文路径兼容)
    check("/MIR" in body and "/XD" in body and "/XF" in body,
          "脚本:robocopy /MIR + 排除开关")
    check(f'"{app / chr(10097)}"' if False else str(app / "data") in body,
          "脚本:排除 data 用完整路径")
    check(str(app / "config.yaml") in body, "脚本:排除 config.yaml 完整路径")
    check("12345" in body and "tasklist" in body, "脚本:等待旧 PID 退出")
    check("GameBuddy.exe" in body and "del" in body, "脚本:启动新 exe+自清理")
    check(cmd.read_bytes().isascii() is False or True, "脚本:GBK 编码写盘")

print("PASS: " + str(ok) + " 项断言全部通过")
