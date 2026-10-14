# -*- coding: utf-8 -*-
"""REQ-20261014-003 顺带补齐的 import_session 契约行为回归（契约 §5.3/§7）。

- session.json 是 manifest 超集：contract_version/pushed_at/missing 继承；
- lines.report（SRV-2 服务端状态）幂等重跑保留，manifest 覆盖不掉；
- 契约主版本不一致 → print 警告 + log.warning（不阻断）；
- events.jsonl 每行 src 客户端绝对路径 → 服务端 sessions/<场次>/video/ 精确重映射。
"""
import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, ".")

from toolbox import transfer


def main():
    with tempfile.TemporaryDirectory() as td:
        t = Path(td)
        data = t / "data"                      # 假 DATA 根（asr/reports 所在）
        real_data = transfer.DATA
        transfer.DATA = data
        try:
            cv = r"C:\client\video\P1.mp4"     # 客户端绝对路径
            rpt = data / "reports" / "rpt1_full"
            rpt.mkdir(parents=True)
            (rpt / "events.jsonl").write_text(
                json.dumps({"kind": "match_start", "t_start": 1.0, "t_end": 1.0,
                            "detail": "d", "confidence": 1.0, "meta": {},
                            "src": cv}, ensure_ascii=False) + "\n",
                encoding="utf-8")
            sdir = t / "root" / "sessions" / "TESTSESS"
            sdir.mkdir(parents=True)
            manifest = {
                "contract_version": "0.9.0",   # 主版本不一致 → 应告警
                "session": "TESTSESS", "mode": "eval",
                "pushed_at": "2026-10-14T00:00:00", "missing": ["asr:P2"],
                "parts": [{"client_video": cv, "filename": "P1.mp4",
                           "asr_dir": None, "events_rpt": "rpt1_full"}],
                "lines": {"knockdowns": "kd1", "pickups": "pk1"},
            }
            (sdir / "manifest.json").write_text(
                json.dumps(manifest, ensure_ascii=False), encoding="utf-8")

            transfer.import_session("TESTSESS", remote_root=str(t / "root"))

            sp = sdir / "session.json"
            state = json.loads(sp.read_text(encoding="utf-8"))
            # 超集继承 + 服务端状态字段
            assert state["contract_version"] == "0.9.0", state
            assert state["pushed_at"] == "2026-10-14T00:00:00"
            assert state["missing"] == ["asr:P2"]
            assert state["video_ready"] is False
            assert state["parts"][0]["server_video"].endswith("P1.mp4")
            # src 重映射
            line = json.loads((rpt / "events.jsonl").read_text(
                encoding="utf-8").splitlines()[0])
            assert line["src"] == str(sdir / "video" / "P1.mp4"), line

            # lines.report 幂等保留：服务端写入后，重跑 import 不得抹掉
            state["lines"]["report"] = "gamerep1"
            sp.write_text(json.dumps(state, ensure_ascii=False, indent=2),
                          encoding="utf-8")
            transfer.import_session("TESTSESS", remote_root=str(t / "root"))
            state2 = json.loads(sp.read_text(encoding="utf-8"))
            assert state2["lines"]["report"] == "gamerep1", state2["lines"]
            assert state2["lines"]["knockdowns"] == "kd1"
        finally:
            transfer.DATA = real_data

    print("import_session 契约行为回归 ✓（超集继承 / lines.report 保留 / "
          "src 重映射 / 版本告警不阻断）")


if __name__ == "__main__":
    main()
