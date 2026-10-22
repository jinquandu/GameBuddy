# -*- coding: utf-8 -*-
"""REQ-20261022-005 回归:环境检测(替换新手教程)。

- 素材:旧教程缓存迁移、SHA-256 校验、下载失败降级
- 结论:六级探测的 ok/warn/bad 判定与整体 ok、30 分钟录像耗时估算
- 落盘:result.json 读写/摘要/详情/状态
- 升级清理:旧教程队列记录+沙盒+素材缓存移除,真实场次不受影响
- UI 侧:环境徽标与登录/权益/微信并列;教程入口已移除
"""
import json
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, ".")

from toolbox import envcheck as ec
from toolbox import folder_watch as fw


def _stub_io(ok_material=True, disk_free=100.0, write=200.0,
             decode_ratio=0.05, ocr_ms=120.0):
    frames = 6
    return {
        "system_info": lambda: {"cpu": 16, "cpu_name": "Test CPU",
                                "ram_gb": 32.0, "os": "Windows 10",
                                "frozen": False},
        "disk_info": lambda wd=None: {"data_free_gb": disk_free,
                                      "watch_free_gb": disk_free,
                                      "write_mbps": write,
                                      "write_error": ""},
        "check_tools": lambda: {"ffmpeg": "ffmpeg version test",
                                "ffprobe": "ffprobe version test",
                                "x264": True, "error": ""},
        "download_material": lambda progress=None: ok_material,
        "probe_media": lambda p: {"duration_sec": 214.0, "width": 1280,
                                  "height": 720},
        "bench_decode": lambda p: {"media_sec": 90, "secs": 90 * decode_ratio,
                                   "ratio": decode_ratio,
                                   "x_realtime": round(1 / decode_ratio, 1)},
        "bench_ocr": lambda p: {"frames": frames, "width": 1280,
                                "height": 720, "ocr_ms": ocr_ms,
                                "texts": 12, "samples_ms": [ocr_ms] * 5},
    }


def main():
    ok = 0

    def check(cond, what):
        nonlocal ok
        assert cond, "FAIL: " + what
        ok += 1
        print("  ✓ " + what)

    with tempfile.TemporaryDirectory() as td:
        t = Path(td)
        real_q, real_dir, real_file = fw.QUEUE_FILE, ec.CHECK_DIR, ec.RESULT_FILE
        real_legacy, real_sha = ec.LEGACY_MATERIAL, ec.DEMO_SHA256
        real_legacy_dir = fw.LEGACY_TUTORIAL_DIR
        fw.QUEUE_FILE = t / "state.json"
        ec.CHECK_DIR = t / "envcheck"
        ec.RESULT_FILE = ec.CHECK_DIR / "result.json"
        fw.LEGACY_TUTORIAL_DIR = t / "data_tutorial"
        try:
            # ---- 六级判定:全部正常 → ok ----
            r = ec.run(io=_stub_io(), user_id="u1")
            check(r["ok"] and r["warn"] == 0 and r["bad"] == 0,
                  "全绿:ok=true,warn=bad=0")
            check([i["key"] for i in r["items"]] ==
                  ["machine", "disk", "tools", "material", "decode", "ocr"],
                  "六项检测项顺序齐全")
            check(r["user_id"] == "u1", "user_id 记入结果")
            est = (r.get("estimate") or {}).get("min_per_30")
            check(est is not None and est > 0,
                  "预计耗时:30 分钟录像估算有值")

            # ---- 慢机器:warn 不阻断 ----
            r2 = ec.run(io=_stub_io(disk_free=5.0, write=10.0,
                                    decode_ratio=0.3, ocr_ms=900.0))
            check(r2["ok"] and r2["warn"] >= 3 and r2["bad"] == 0,
                  "慢机器:warn 多项但仍可用(ok=true)")
            levels = {i["key"]: i["level"] for i in r2["items"]}
            check(levels["disk"] == "warn" and levels["decode"] == "warn"
                  and levels["ocr"] == "warn", "慢机器 warn 落在磁盘/解码/识别")

            # ---- 不可用:磁盘过小/识别过慢 → ok=false ----
            r3 = ec.run(io=_stub_io(disk_free=0.5, ocr_ms=3000.0))
            levels = {i["key"]: i["level"] for i in r3["items"]}
            check(not r3["ok"] and r3["bad"] == 2 and
                  levels["disk"] == "bad" and levels["ocr"] == "bad",
                  "不可用:磁盘/识别 bad 时 ok=false")
            check("发现问题" in ec.summary_line(r3),
                  "摘要行:不可用时给出问题项")

            # ---- 素材下载失败:素材/解码/识别三项降级为 bad ----
            r4 = ec.run(io=_stub_io(ok_material=False))
            levels = {i["key"]: i["level"] for i in r4["items"]}
            check(not r4["ok"] and levels["material"] == "bad"
                  and levels["decode"] == "bad" and levels["ocr"] == "bad",
                  "素材下载失败:相关项 bad,整体不可用")

            # ---- 落盘与读取 ----
            check(ec.last_result().get("checked_at") == r3["checked_at"],
                  "last_result:读取最近一次落盘结果")
            check(ec.result_state() == "fail", "result_state:失败态")
            check(ec.result_state({"ok": True, "warn": 0}) == "pass",
                  "result_state:通过态")
            check(ec.result_state({"ok": True, "warn": 2}) == "warn",
                  "result_state:提示态")
            check(ec.result_state({}) == "off", "result_state:未检测态")
            check(len(ec.detail_lines(r3)) == 6, "详情行:六项")
            check("30 分钟" in ec.summary_line(r or {"ok": True, "warn": 0,
                                                     "items": [],
                                                     "checked_at": "2026-10-22 10:00:00"}),
                  "摘要行:通过时给出预计识别耗时" if est else "摘要行")

            # ---- 旧教程素材迁移 ----
            old = t / "oldtut" / "demo-input.mp4"
            old.parent.mkdir(parents=True)
            old.write_bytes(b"demo-bytes")
            fake_sha = __import__("hashlib").sha256(b"demo-bytes").hexdigest()
            ec.DEMO_SHA256 = fake_sha
            ec.LEGACY_MATERIAL = old
            try:
                ec.CHECK_DIR = t / "envcheck2"
                ec.RESULT_FILE = ec.CHECK_DIR / "result.json"
                check(ec.adopt_legacy_material()
                      and ec.material_ready(), "旧教程素材迁移:校验通过即复用")
                ec.material_path().write_bytes(b"corrupt")
                check(not ec.material_ready(), "素材 SHA-256:内容被改即拒绝")
            finally:
                ec.LEGACY_MATERIAL, ec.DEMO_SHA256 = old, real_sha

            # ---- 升级清理:旧教程遗留(队列+沙盒+缓存) ----
            ec.CHECK_DIR = real_dir
            ec.RESULT_FILE = real_file
            old_cache = t / "data_tutorial"
            old_cache.mkdir()
            (old_cache / "done.json").write_text("{}", encoding="utf-8")
            sandbox = t / "watch" / fw.LEGACY_TUTORIAL_SESSION
            sandbox.mkdir(parents=True)
            (sandbox / "新手体验-demo.mp4").write_bytes(b"v" * 100)
            keep_dir = t / "watch" / "真实场次B"
            keep_dir.mkdir()
            (keep_dir / "P1.mp4").write_bytes(b"v" * 100)
            fw._save({"jobs": {
                "k_legacy": {"key": "k_legacy", "path": str(sandbox),
                             "session": fw.LEGACY_TUTORIAL_SESSION,
                             "state": "done", "attempts": 1,
                             "enqueued_at": "2026-10-22 10:00:00",
                             "updated_at": "2026-10-22 10:00:00",
                             "note": ""},
                "k_real": {"key": "k_real", "path": str(keep_dir),
                           "session": "真实场次B", "state": "queued",
                           "attempts": 0,
                           "enqueued_at": "2026-10-22 10:01:00",
                           "updated_at": "2026-10-22 10:01:00",
                           "note": ""}}})
            n = fw._purge_legacy_tutorial()
            jobs = fw._load()["jobs"]
            check(n == 1 and "k_legacy" not in jobs and "k_real" in jobs,
                  "升级清理:旧教程队列记录移除,真实场次保留")
            check(not sandbox.exists(), "升级清理:沙盒目录已删")
            check(not old_cache.exists(), "升级清理:旧教程素材缓存已删")
            check(fw._purge_legacy_tutorial() == 0, "升级清理幂等")
            check(not hasattr(fw, "TUTORIAL_SESSION"),
                  "folder_watch 不再暴露教程会话常量")
        finally:
            fw.QUEUE_FILE = real_q
            fw.LEGACY_TUTORIAL_DIR = real_legacy_dir
            ec.CHECK_DIR, ec.RESULT_FILE = real_dir, real_file
            ec.LEGACY_MATERIAL, ec.DEMO_SHA256 = real_legacy, real_sha
    print("PASS: " + str(ok) + " 项断言全部通过")


if __name__ == "__main__":
    main()
