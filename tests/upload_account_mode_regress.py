# -*- coding: utf-8 -*-
"""账号模式上传回归（REQ-003 拆解 3.6，用户 2026-10-20 批准方案②）。

- 门控：upload_url 已配 + 无 upload_token + 未登录 → 明确报错（不再要求
  人工 token）；已登录 → 放行；
- 头组装：账号模式 ProductionUploader(token=会话 bearer, signer=DSJ-PC-1)
  发出的请求同时带 Authorization: Bearer 与五个 X-DSJ-Device-* 签名头。
"""
import sys
import urllib.request

sys.path.insert(0, ".")

import toolbox.transfer as tr
from toolbox.account import NotLoggedIn
from toolbox.uploader import ProductionUploader

REAL = tr.load_config


def _cfg(token=""):
    return {"server": {"upload_url": "https://x/api/production/v1",
                       "upload_token": token}}


def main():
    # 1) 门控：未登录 → 友好报错（不提人工 token）
    tr.load_config = lambda: dict(_cfg(), extra=None) or _cfg()
    import toolbox.account as acct
    real_rl = acct.require_login
    acct.require_login = lambda *a, **k: (_ for _ in ()).throw(NotLoggedIn())
    try:
        try:
            tr.push("某场次")
            raise AssertionError("未登录应被拦")
        except RuntimeError as e:
            assert "账号模式" in str(e) and "登录" in str(e), e
            assert "upload_token（HTTPS 通道不回退" not in str(e), "旧文案残留"
        print("  ✓ 未登录被拦（账号模式文案）")
    finally:
        acct.require_login = real_rl
        tr.load_config = REAL

    # 2) 头组装：bearer + 五签名头同发
    from toolbox import device_identity as di
    sess = {"bearer": "SESSBEARER123"}
    up = ProductionUploader(
        "https://x/api/production/v1", token=sess["bearer"],
        signer=lambda m, w, d: di.sign_request(m, w, d, sess["bearer"]))
    captured = {}
    class FakeResp:
        def read(self): return b"{}"
        def __enter__(self): return self
        def __exit__(self, *a): return False
    def fake_urlopen(req, timeout=None):
        captured["headers"] = dict(req.headers)
        captured["url"] = req.full_url
        return FakeResp()
    real_uo = urllib.request.urlopen
    urllib.request.urlopen = fake_urlopen
    try:
        up._request("GET", "/capabilities")
    finally:
        urllib.request.urlopen = real_uo
    h = {k.title(): v for k, v in captured["headers"].items()}
    assert h.get("Authorization") == "Bearer SESSBEARER123", h
    for k in ("X-Dsj-Device-Id", "X-Dsj-Device-Key", "X-Dsj-Device-Time",
              "X-Dsj-Device-Nonce", "X-Dsj-Device-Signature"):
        assert k in h, f"缺签名头 {k}: {h}"
    print("  ✓ 账号模式请求头：Bearer 会话 + 五签名头齐发")
    print("PASS: 账号模式上传（方案②）门控与头组装回归通过")


if __name__ == "__main__":
    main()
