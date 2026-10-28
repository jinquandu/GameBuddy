# -*- coding: utf-8 -*-
"""REQ-20261028-001 回归:ffprobe 空输出防御与环境检测第 6 步去 probe 化。

现场(2026-10-28 客户机):probe_video 的 ffprobe stdout 未被捕获,
json.loads(None) 裸 TypeError 把环境检测第 6 步整个打掉。修复后:
- probe_video 对空输出/非零退出/坏 JSON 重试一次,再抛可读 RuntimeError
  (不再出现 Python 原生 TypeError);
- _extract_frames/bench_ocr 支持 size=(w,h) 已知尺寸,跳过 ffprobe 预探测;
- 第 6 步在 run() 里固定传素材常量尺寸。
"""
import subprocess
import sys
from subprocess import CompletedProcess

sys.path.insert(0, ".")
sys.stdout.reconfigure(encoding="utf-8")

from toolbox import envcheck as ec
from toolbox import ingest


def main():
    ok = 0

    def check(cond, what):
        nonlocal ok
        assert cond, "FAIL: " + what
        ok += 1
        print("  ✓ " + what)

    real_run = subprocess.run
    real_probe_media = ec.probe_media

    def install(fake):
        subprocess.run = fake

    try:
        # ---- 空输出(现场案):rc=0 但 stdout=None → 可读报错,且重试过 ----
        calls = []

        def fake_run_none(*a, **kw):
            calls.append(a)
            return CompletedProcess(a[0], 0, stdout=None, stderr=None)

        install(fake_run_none)
        try:
            ingest.probe_video("x.mp4")
            raise AssertionError("should raise")
        except RuntimeError as e:
            check("无标准输出" in str(e) and "白名单" in str(e),
                  "空输出:RuntimeError 提示白名单,非裸 TypeError")
            check("TypeError" not in str(e), "空输出:报错不再是 Python TypeError")
        check(len(calls) == 2, "空输出:默认重试一次(共 2 次调用)")

        # ---- 首次空输出、重试成功 → 正常返回(瞬时干扰可自愈) ----
        seq = {"i": 0}
        good = ('{"streams": [{"codec_type": "video", "width": 1280,'
                ' "height": 720}], "format": {"duration": "214.02"}}')

        def fake_run_recovers(*a, **kw):
            seq["i"] += 1
            if seq["i"] == 1:
                return CompletedProcess(a[0], 0, stdout=None, stderr=None)
            return CompletedProcess(a[0], 0, stdout=good, stderr="")

        install(fake_run_recovers)
        d = ingest.probe_video("x.mp4")
        check(d == {"duration_sec": 214.0, "width": 1280, "height": 720},
              "瞬时干扰:重试后正常解析返回")

        # ---- 非零退出:报错带 stderr(旧语义保留) ----
        def fake_run_rc(*a, **kw):
            return CompletedProcess(a[0], 1, stdout="", stderr="boom-detail")

        install(fake_run_rc)
        try:
            ingest.probe_video("x.mp4")
            raise AssertionError("should raise")
        except RuntimeError as e:
            check("boom-detail" in str(e) and "退出码 1" in str(e),
                  "非零退出:RuntimeError 带退出码与 stderr")

        # ---- 坏 JSON:同样走重试+可读报错 ----
        def fake_run_badjson(*a, **kw):
            return CompletedProcess(a[0], 0, stdout="not-json", stderr="")

        install(fake_run_badjson)
        try:
            ingest.probe_video("x.mp4")
            raise AssertionError("should raise")
        except RuntimeError as e:
            check("不是有效 JSON" in str(e), "坏 JSON:RuntimeError 可读报错")

        # ---- _extract_frames(size=...):不再调 probe_media ----
        def probe_must_not_run(p):
            raise AssertionError("probe_media should be skipped with size")

        ec.probe_media = probe_must_not_run

        def fake_run_raw(*a, **kw):
            # 2 帧 × (4×2×3) 字节 BGR24
            return CompletedProcess(a[0], 0, stdout=b"\1" * 48, stderr=b"")

        install(fake_run_raw)
        frames, w, h = ec._extract_frames("x.mp4", n=2, size=(4, 2))
        check((w, h) == (4, 2) and frames.shape == (2, 2, 4, 3),
              "_extract_frames:size 直传,跳过 probe,帧形状正确")
    finally:
        subprocess.run = real_run
        ec.probe_media = real_probe_media

    print("PASS: " + str(ok) + " 项断言全部通过")


if __name__ == "__main__":
    main()
