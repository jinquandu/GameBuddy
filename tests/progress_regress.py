# -*- coding: utf-8 -*-
"""进度条模型回归（2026-10-20 用户需求：按时长估时+真实进度，替换 x/9）。

ProgressParser 喂真实 pipeline 行格式（合成），校验：
- detect 按分片时长比例推进（长片贡献大）；
- 跳过阶段（asr/speaker）权重自然计入已完成；
- knockdowns 按「：N 粗簇」分片行推进；
- 全链完成=100%；未完成封顶 99%；
- eta_sec 外推单调合理；无时长表退化为按片数。
"""
import sys
import time

sys.path.insert(0, ".")

from toolbox.pet import STAGES, ProgressParser

VIDS = {f"场次A P{k} 20日1{k}点00分.mp4": d
        for k, d in enumerate((1800, 600, 600), start=1)}   # 共 50 分钟


def feed(p, *lines):
    for l in lines:
        p.update(l)
    return p


def main():
    p = ProgressParser()
    p.set_media(VIDS)
    p.update("[pipeline] 场次：场次A，3 个分片")

    # asr/speaker 跳过（核心包）→ 权重计入已完成
    p.update("[pipeline] -- 跳过 asr")
    p.update("[pipeline] -- 跳过 speaker")
    f0, _ = p.progress()
    assert 0.03 < f0 < 0.07, f0                       # 0.02+0.03+0.01≈0.06

    # detect：P1（36 分钟，占 72%）完成 → detect 进度 ≈72%
    p.update("[pipeline] detect 开始 ...")
    p.update("  场次A P1 20日11点00分.mp4：检测完成 -> rpt（21 条）")
    f1, cur = p.progress()
    assert cur == "detect"
    expect = f0 + 0.75 * 1800 / 3000
    assert abs(f1 - expect) < 1e-6, (f1, expect)
    # 复用行同样计进度且不重复计
    p.update("  场次A P1 20日11点00分.mp4：复用事件流 rpt（21 条）")
    assert abs(p.progress()[0] - f1) < 1e-9, "同片重复计数"
    # P2/P3（各 12 分钟）→ detect 满程
    p.update("  场次A P2 20日12点00分.mp4：检测完成 -> rpt（9 条）")
    p.update("  场次A P3 20日13点00分.mp4：检测完成 -> rpt（7 条）")
    p.update("[pipeline] detect 完成 (610s)")
    fd, _ = p.progress()
    assert fd == f0 + 0.75, fd

    # knockdowns：按粗簇行推进（3 片各一行）
    p.update("[pipeline] knockdowns 开始 ...")
    p.update("  场次A P1 20日11点00分.mp4：6 粗簇 -> 4 会话")
    fk1, _ = p.progress()
    assert abs(fk1 - (fd + 0.10 / 3)) < 1e-6, fk1
    p.update("  场次A P2 20日12点00分.mp4：0 粗簇 -> 0 会话")
    p.update("  场次A P3 20日13点00分.mp4：2 粗簇 -> 1 会话")
    p.update("[pipeline] knockdowns 完成 (30s)")

    # 其余阶段：中程 50% → 完成 100% 封顶 0.99
    for name, _ in STAGES:
        if name in ("download", "asr", "speaker", "detect", "knockdowns"):
            continue
        p.update(f"[pipeline] {name} 开始 ...")
        fmid, _ = p.progress()
        p.update(f"[pipeline] {name} 完成 (5s)")
    assert abs(p.progress()[0] - 0.99) < 1e-9, "未完成应封顶 0.99"
    p.update("[pipeline] 全链完成。")
    assert p.progress()[0] == 1.0
    assert p.eta_sec() == 0

    # ETA：核心包会先跳过 asr/speaker；P1 完成（detect 半程）处已用 10 分钟
    p2 = ProgressParser()
    p2.set_media({"a P1 1日10点00分.mp4": 600, "a P2 1日10点10分.mp4": 600})
    p2.update("[pipeline] 场次：a，2 个分片")
    p2.update("[pipeline] -- 跳过 asr")
    p2.update("[pipeline] -- 跳过 speaker")
    p2.update("[pipeline] detect 开始 ...")
    p2.update("  a P1 1日10点00分.mp4：检测完成 -> r（5 条）")
    p2.t0 = time.time() - 600              # 假装已跑 10 分钟到此处
    frac = 0.02 + 0.03 + 0.01 + 0.75 * 0.5  # = 0.435
    eta = p2.eta_sec()
    assert abs(eta - 600 * (1 - frac) / frac) < 30, eta

    # 无时长表：退化按片数
    p3 = ProgressParser()
    p3.update("[pipeline] 场次：b，2 个分片")
    p3.update("[pipeline] detect 开始 ...")
    p3.update("  b P1 1日10点00分.mp4：检测完成 -> r（3 条）")
    f, _ = p3.progress()
    assert abs(f - (0.75 * 0.5 + 0.02)) < 1e-6, f

    print("PASS: 进度模型回归全部通过（时长加权/跳过计入/分片推进/封顶/ETA/退化）")


if __name__ == "__main__":
    main()
