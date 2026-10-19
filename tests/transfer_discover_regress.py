# -*- coding: utf-8 -*-
"""discover_session / _find_events_rpt / 租户自动分配 契约回归
（2026-10-15 exe 实测 P1-2/P2-1 + 2026-10-19 auto-tenant）。

- 空事件流报告 = 0 事件结论：不算 missing（曾把 0 事件的 P2/P3 登记成
  「无事件流（先跑 toolbox detect）」→ manifest 误标 incomplete，打标站长
  期误导）；
- legacy 裸名目录仍按 src 匹配；
- missing 文案用全名（曾 [:30] 把长中文名连扩展名截掉写进 manifest.json）；
- server.tenant 未配置 → auto-YYYYMMDD-HHMMSS 生成 + config.yaml 持久化 +
  二次复用；显式配置优先；占位空值原位替换；
- _ssh 显式 UTF-8 解码（冻结 exe locale=GBK 曾炸 reader 线程致 stdout=None）。

不依赖网络/OCR：REPORTS/ASR/ROOT 指到临时目录。
跑法：py -3 tests/transfer_discover_regress.py
"""
import json
import re
import subprocess as sp
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

            # ---- auto-tenant：未配置 → 生成/持久化/复用 ----
            import toolbox.config as cfgmod
            real_root = cfgmod.ROOT
            troot = t / "fake_root"
            troot.mkdir()
            cfgmod.ROOT = troot          # _user_config/_persist_tenant 均按调用时 ROOT 取
            try:
                cfg = {"remote_root": "/opt/dashijie-eval-data"}
                got = transfer._ensure_tenant(cfg)
                assert re.fullmatch(r"auto-\d{8}-\d{6}", got), got
                assert cfg["tenant"] == got              # 本进程内即时生效
                written = (troot / "config.yaml").read_text(encoding="utf-8")
                assert f'tenant: "{got}"' in written, written
                # 二次（等价新进程重新读盘）→ 复用持久化值，不再生成
                assert transfer._ensure_tenant({"remote_root": "/x"}) == got
                assert transfer._tenant_root(
                    {"remote_root": "/opt/x"}) == f"/opt/x/tenants/{got}"
                # 用户显式配置优先（数值租户 007 也能原样返回）
                (troot / "config.yaml").write_text(
                    'server:\n  tenant: "007"\n', encoding="utf-8")
                assert transfer._ensure_tenant({}) == "007"
                # 占位空值 → 原位替换，不产生第二行 tenant
                (troot / "config.yaml").write_text(
                    "server:\n  tenant:\n  ssh_alias: ecs\n", encoding="utf-8")
                got3 = transfer._ensure_tenant({})
                assert re.fullmatch(r"auto-\d{8}-\d{6}", got3), got3
                text3 = (troot / "config.yaml").read_text(encoding="utf-8")
                assert f'tenant: "{got3}"' in text3, text3
                assert len(re.findall(r"^\s*tenant\s*:", text3, re.M)) == 1
                assert "ssh_alias: ecs" in text3       # 其余行不动
            finally:
                cfgmod.ROOT = real_root

            # ---- _ssh 显式 UTF-8：不随 locale 解码 ----
            real_run = sp.run
            cap = {}

            def fake_run(cmd, **kw):
                cap.update(kw)
                cap["cmd"] = cmd
                return sp.CompletedProcess(cmd, 0, stdout="ok", stderr="")

            sp.run = fake_run
            try:
                transfer._ssh("dummy", "echo hi", check=False)
            finally:
                sp.run = real_run
            assert cap.get("encoding") == "utf-8", cap
            assert cap.get("errors") == "replace", cap
        finally:
            transfer.REPORTS, transfer.DATA = real_reports, real_data
            asr_mod.ASR = real_asr

    print("discover_session/_find_events_rpt/auto-tenant/_ssh 回归 ✓（空报告="
          "结论不进 missing / legacy src 匹配 / missing 全名 / auto 租户生成-"
          "持久化-复用 / _ssh 恒 UTF-8）")


if __name__ == "__main__":
    main()
