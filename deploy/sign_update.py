# -*- coding: utf-8 -*-
"""发布侧更新清单签名工具(REQ-20261026-002)。

用法(发布机,私钥不进仓库):
    py -3 deploy/sign_update.py --pkg dist/GameBuddy-core.zip \
        --build 20261101-r8 --version 0.3.0 \
        --notes "修复…;新增…" [--min-required 0.2.0] \
        [--url /eval/clients/dashijie-pet-20261101-r8.zip] \
        [--key C:\\path\\to\\private.pem] [--disabled]

    私钥路径也可用环境变量 DSJ_UPDATE_SIGN_KEY 指定。
    输出:签名好的 client-update.json 到 stdout(重定向或直接看),
    把它 scp 到 /opt/dashijie-eval-data/clients/client-update.json。

canonical/验签与客户端共用 toolbox.update_signing——两边漂移会在
客户端验签失败时立刻暴露,不会静默通过。
"""
import argparse
import hashlib
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from toolbox import update_signing  # noqa: E402


def sha256_of(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for b in iter(lambda: f.read(1 << 20), b""):
            h.update(b)
    return h.hexdigest()


def main(argv=None):
    ap = argparse.ArgumentParser(description="签名更新清单")
    ap.add_argument("--pkg", required=True, help="安装包 zip 路径")
    ap.add_argument("--version", required=True, help="语义版本,如 0.3.0")
    ap.add_argument("--build", required=True, help="构建号,如 20261101-r8")
    ap.add_argument("--notes", default="", help="更新说明(客户可见)")
    ap.add_argument("--min-required", default="", help="最低要求版本(可空)")
    ap.add_argument("--url", default="", help="包直链(相对 /eval/... 或绝对;"
                                             "缺省按包名生成)")
    ap.add_argument("--key", default="", help="私钥 PEM 路径"
                                             "(或环境变量 DSJ_UPDATE_SIGN_KEY)")
    ap.add_argument("--disabled", action="store_true",
                    help="置 channel_disabled=true(停用更新通道)")
    ap.add_argument("--out", default="", help="输出文件(缺省 stdout)")
    a = ap.parse_args(argv)

    pkg = Path(a.pkg)
    if not pkg.is_file():
        raise SystemExit(f"包不存在: {pkg}")
    key_path = a.key or ""
    if not key_path:
        import os
        key_path = os.environ.get("DSJ_UPDATE_SIGN_KEY", "")
    if not key_path:
        raise SystemExit("缺 --key 或 DSJ_UPDATE_SIGN_KEY(私钥离线保管)")
    key_bytes = Path(key_path).read_bytes()

    url = a.url or f"/eval/clients/{pkg.name}"
    latest = {
        "version": a.version,
        "build": a.build,
        "url": url,
        "sha256": sha256_of(pkg),
        "size": pkg.stat().st_size,
        "notes": a.notes,
        "min_required": a.min_required,
        "released_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    }
    sig = update_signing.sign(a.disabled, latest, key_bytes)
    manifest = {
        "channel_disabled": bool(a.disabled),
        "latest": latest,
        "sig": sig,
    }
    text = json.dumps(manifest, ensure_ascii=False, indent=2)
    if a.out:
        Path(a.out).write_text(text, encoding="utf-8")
        print(f"written {a.out}")
    else:
        print(text)
    # 自验:用内置公钥回验一遍,发布前兜底
    ok = update_signing.verify(a.disabled, latest, "", sig)
    print(f"self-verify: {'OK' if ok else 'FAIL'}", file=sys.stderr)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
