# -*- coding: utf-8 -*-
"""discover_session / _find_events_rpt 契约回归（2026-10-15 exe 实测 P1-2/P2-1）。

- 空事件流报告 = 0 事件结论：不算 missing（曾把 0 事件的 P2/P3 登记成
  「无事件流（先跑 toolbox detect）」→ manifest 误标 incomplete，打标站长
  期误导）；
- legacy 裸名目录仍按 src 匹配；
- missing 文案用全名（曾 [:30] 把长中文名连扩展名截掉写进 manifest.json）。

不依赖网络/OCR：REPORTS/ASR 指到临时目录。
跑法：py -3 tests/transfer_discover_regress.py
"""
import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, ".")

from toolbox import transfer
from toolbox.common import collect_videos, slug


def main():
    with tempfile.TemporaryDirectory() as td:
        t = Path(td)
        reports = t / "reports"
        asr_root = t / "asr"
        reports.mkdir(parents=True)
        asr_root.mkdir(parents=True)
        real_reports, real_data = transfer.REPORTS, transfer.DATA
        import toolbox.asr as asr_mod
        real_asr = asr_mod.ASR
        transfer.REPORTS = reports
        transfer.DATA = t / "data"
        asr_mod.ASR = asr_root
        try:
            sess = t / "重生之0资产账号 甜药Jamren"
            sess.mkdir()
            p1 = sess / "重生之0资产账号 甜药Jamren P1 8日13点09分.mp4"
            p2 = sess / "重生之0资产账号 甜药Jamren P2 8日13点39分.mp4"
            p3 = sess / "重生之0资产账号 甜药Jamren P3 8日14点09分.mp4"
            for p in (p1, p2, p3):
                p.write_bytes(b"")               # collect_videos 只看扩展名
            v1, v2, v3 = collect_videos(sess)

            # P1：规范目录非空报告 + asr 在
            conv1 = reports / (slug(v1.stem) + "_full")
            conv1.mkdir(parents=True)
            (conv1 / "events.jsonl").write_text(
                json.dumps({"kind": "down", "t_start": 1, "t_end": 2,
                            "detail": "d", "confidence": 0.9,
                            "src": str(v1)}, ensure_ascii=False) + "\n",
                encoding="utf-8")
            adir = asr_root / "asr_p1"
            adir.mkdir()
            (adir / "transcript.json").write_text(
                json.dumps({"game": str(v1)}, ensure_ascii=False),
                encoding="utf-8")

            # P2：规范目录空报告（0 事件结论）——不算 missing
            conv2 = reports / (slug(v2.stem) + "_full")
            conv2.mkdir(parents=True)
            (conv2 / "events.jsonl").write_text("", encoding="utf-8")

            # P3：legacy 裸名目录，src 匹配（回归：老数据仍能发现）
            legacy = reports / "旧时代裸名"
            legacy.mkdir()
            (legacy / "events.jsonl").write_text(
                json.dumps({"kind": "down", "t_start": 3, "t_end": 4,
                            "detail": "d", "confidence": 0.9,
                            "src": str(v3)}, ensure_ascii=False) + "\n",
                encoding="utf-8")

            assert transfer._find_events_rpt(v1) == conv1
            assert transfer._find_events_rpt(v2) == conv2, "空规范报告=0事件结论"
            assert transfer._find_events_rpt(v3) == legacy

            disc = transfer.discover_session(sess)
            miss = "\n".join(disc["missing"])
            # P1/P2/P3 事件流都在 → 一条「无事件流」都不该有
            assert "无事件流" not in miss, disc["missing"]
            # P2/P3 无 asr → missing 如实登记，且文件名用全名（P2-1）
            assert f"{v2.name}：无 ASR 转写（先跑 toolbox asr）" in miss, disc["missing"]
            assert f"{v3.name}：无 ASR 转写（先跑 toolbox asr）" in miss, disc["missing"]
            assert len(disc["missing"]) == 2, disc["missing"]
            by_file = {p["filename"]: p for p in disc["parts"]}
            assert by_file[v1.name]["events_rpt"] == conv1.name
            assert by_file[v2.name]["events_rpt"] == conv2.name, "空报告也要进 parts"
            assert by_file[v1.name]["asr_dir"] == adir.name

            # 反例：报告不存在时仍如实 missing（空文件≠不存在）
            p4 = sess / "重生之0资产账号 甜药Jamren P4 8日15点09分.mp4"
            p4.write_bytes(b"")
            disc2 = transfer.discover_session(sess)
            miss2 = "\n".join(disc2["missing"])
            assert f"{p4.name}：无事件流（先跑 toolbox detect）" in miss2, \
                disc2["missing"]
        finally:
            transfer.REPORTS, transfer.DATA = real_reports, real_data
            asr_mod.ASR = real_asr

    print("discover_session/_find_events_rpt 回归 ✓（空报告=结论不进 missing / "
          "legacy src 匹配 / missing 全名）")


if __name__ == "__main__":
    main()
