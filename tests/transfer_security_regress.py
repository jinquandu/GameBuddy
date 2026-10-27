# -*- coding: utf-8 -*-
"""安全审计 2026-10-26 P0 修复回归（H-03 / H-05 / M-04，REQ-20261027-001）。

- import_session：manifest 路径字段（parts[].filename/asr_dir/events_rpt、
  lines.knockdowns/pickups/voice[]、场次名）越界 → 整体拒绝，服务端零读写；
- import_session：events.jsonl 行 src 只认本会话已登记视频——客户端路径重映射、
  已是服务端路径幂等保留、其余（file:///他租户、http://… 等）剔除；
- sessions prune：session.json 内越界路径字段 → 跳过留痕，data 树外目录
  绝不 rmtree；合法产物照常连带删除；
- remix.discover_pool：服务端模式（TOOLBOX_DATA_DIR）下 src 未落在租户根
  之内 / 协议 URL 形态 → 剔除不进池；端侧本地路径（含 Windows 盘符）放行。
"""
import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, ".")

from toolbox import remix, transfer


def _mk_manifest(cv, **over):
    m = {"contract_version": "1.3.3", "session": "SESS", "mode": "eval",
         "pushed_at": "2026-10-27T00:00:00", "missing": [],
         "parts": [{"client_video": cv, "filename": "P1.mp4",
                    "asr_dir": None, "events_rpt": "rpt1_full"}],
         "lines": {"knockdowns": None, "pickups": None, "voice": []}}
    m.update(over)
    return m


def test_safe_component():
    ok = ["P1 猪猪夏 18日.mp4", "rpt1_full", "kd_中文-目录", "a" * 200,
          "20261026_场次（测试）[1]"]
    for v in ok:
        transfer._safe_component(v, "t")
    bad = ["../evil", "..\\..\\evil", "a/b", "a\\b", "C:/evil", "C:\\evil",
           "evil:ads", "..", ".", "", "a\nb", "a\x01b", "a" * 300]
    for v in bad:
        try:
            transfer._safe_component(v, "t")
        except ValueError:
            continue
        raise AssertionError(f"_safe_component 应拒绝 {v!r}")
    print("  _safe_component 成分白名单 ✓")


def test_import_session_rejects_traversal():
    with tempfile.TemporaryDirectory() as td:
        t = Path(td)
        data = t / "data"
        real_data = transfer.DATA
        transfer.DATA = data
        try:
            # 攻击目标：DATA/asr 之外的目录（asr_dir=../../evil 指向这里）
            evil = t / "evil"
            (evil / "x").mkdir(parents=True)
            sentinel = evil / "transcript.json"
            sentinel.write_text('{"game": "G", "sentinel": true}', encoding="utf-8")
            sdir = t / "root" / "sessions" / "SESS"
            sdir.mkdir(parents=True)

            cases = {
                "parts[].asr_dir": lambda m: m["parts"][0].update(
                    asr_dir="../../evil"),
                "parts[].events_rpt": lambda m: m["parts"][0].update(
                    events_rpt="..\\..\\evil"),
                "parts[].filename": lambda m: m["parts"][0].update(
                    filename="../evil.mp4"),
                "lines.knockdowns": lambda m: m["lines"].update(
                    knockdowns="../../victim"),
                "lines.voice": lambda m: m["lines"].update(
                    voice=["../voice_evil"]),
            }
            for field, mutate in cases.items():
                m = _mk_manifest(r"C:\c\v.mp4")
                mutate(m)
                (sdir / "manifest.json").write_text(
                    json.dumps(m, ensure_ascii=False), encoding="utf-8")
                try:
                    transfer.import_session("SESS", remote_root=str(t / "root"))
                except ValueError as ex:
                    assert field in str(ex), (field, ex)
                else:
                    raise AssertionError(f"应拒绝越界字段 {field}")
                assert not (sdir / "session.json").is_file(), \
                    f"{field}: 拒绝后不得写 session.json"
            # 攻击目标原样未动
            assert json.loads(sentinel.read_text(encoding="utf-8"))["sentinel"]
            # 场次名本身带分隔符也拒绝
            try:
                transfer.import_session("../other", remote_root=str(t / "root"))
            except ValueError:
                pass
            else:
                raise AssertionError("应拒绝越界场次名")
        finally:
            transfer.DATA = real_data
    print("  import_session 路径遍历整体拒绝 ✓")


def test_import_session_src_normalize():
    with tempfile.TemporaryDirectory() as td:
        t = Path(td)
        data = t / "data"
        real_data = transfer.DATA
        transfer.DATA = data
        try:
            cv = r"C:\client\video\P1.mp4"
            rpt = data / "reports" / "rpt1_full"
            rpt.mkdir(parents=True)
            sv = str(t / "root" / "sessions" / "SESS" / "video" / "P1.mp4")
            rows = [
                {"kind": "down", "src": cv, "t": 1},                       # 客户端路径→重映射
                {"kind": "down", "src": "http://attacker/x.mp4", "t": 2},  # 越界→剔除
                {"kind": "down", "src": "/etc/passwd", "t": 3},            # 越界→剔除
                {"kind": "down", "src": sv, "t": 4},                       # 已是服务端路径→保留
            ]
            (rpt / "events.jsonl").write_text(
                "\n".join(json.dumps(r, ensure_ascii=False) for r in rows),
                encoding="utf-8")
            sdir = t / "root" / "sessions" / "SESS"
            sdir.mkdir(parents=True)
            (sdir / "manifest.json").write_text(
                json.dumps(_mk_manifest(cv), ensure_ascii=False), encoding="utf-8")

            transfer.import_session("SESS", remote_root=str(t / "root"))

            out = [json.loads(l) for l in
                   (rpt / "events.jsonl").read_text(encoding="utf-8").splitlines()
                   if l.strip()]
            assert len(out) == 2, out                     # 越界 2 行被剔除
            assert out[0]["src"] == sv, out[0]            # 客户端路径已重映射
            assert out[1]["src"] == sv, out[1]            # 服务端路径原样保留
        finally:
            transfer.DATA = real_data
    print("  import_session src 归一（重映射/剔除/幂等保留）✓")


def test_prune_containment():
    with tempfile.TemporaryDirectory() as td:
        t = Path(td)
        root = t / "remote"
        t1 = root / "tenants" / "t1"
        data = t1 / "data"
        # 合法 data 产物 + 越界目标（data/victim = knockdowns/../victim 的落点）
        for sub in ("pickups/pk_ok", "voice/voice_ok", "victim"):
            (data / sub).mkdir(parents=True)
            (data / sub / "keep.txt").write_text("x", encoding="utf-8")
        old = t1 / "sessions" / "old"
        new = t1 / "sessions" / "new"
        poisoned = {
            "imported_at": "2026-01-01T00:00:00",
            "parts": [{"asr_dir": "../../evil_asr", "events_rpt": "..\\e",
                       "filename": "x.mp4"}],
            "lines": {"knockdowns": "../victim", "pickups": "pk_ok",
                      "voice": ["C:/evil_voice", "voice_ok"],
                      "report": "../rpt_evil"},
        }
        old.mkdir(parents=True)
        (old / "session.json").write_text(
            json.dumps(poisoned, ensure_ascii=False), encoding="utf-8")
        new.mkdir(parents=True)
        (new / "session.json").write_text(
            json.dumps({"imported_at": "2026-06-01T00:00:00", "lines": {}},
                       ensure_ascii=False), encoding="utf-8")

        env_key = "TOOLBOX_REMOTE_ROOT"
        prev = os.environ.get(env_key)
        os.environ[env_key] = str(root)
        try:
            transfer.sessions(action="prune", keep=1)
        finally:
            if prev is None:
                os.environ.pop(env_key, None)
            else:
                os.environ[env_key] = prev

        assert not old.exists(), "被 prune 会话目录应删除"
        assert new.exists(), "保留会话不受影响"
        assert (data / "victim" / "keep.txt").is_file(), \
            "越界字段（knockdowns=../victim）指向的目录绝不能被删"
        assert not (data / "pickups" / "pk_ok").exists(), "合法 pickups 应连带删除"
        assert not (data / "voice" / "voice_ok").exists(), "合法 voice 应连带删除"
    print("  prune rmtree 夹紧（越界跳过 / 合法照删）✓")


def test_remix_src_guard():
    # URL 形态判定：协议一律拒，Windows 盘符放行
    for url in ("http://a/x.mp4", "https://a", "tcp://1.2.3.4:8080", "pipe:0",
                "md5:abc", "concat:1.txt|2.txt", "file:/etc/passwd",
                "subfile,http://a/x"):
        assert remix._looks_like_url(url), url
    for path in (r"C:\Users\q\a.mp4", "D:", "/opt/x.mp4", "data/rel.mp4",
                 "../rel.mp4", "a.mp4"):
        assert not remix._looks_like_url(path), path

    with tempfile.TemporaryDirectory() as td:
        t = Path(td)
        data = t / "data"
        data.mkdir()
        good = str(t / "sessions" / "s1" / "video" / "a.mp4")
        outside = str(t.parent / "other.mp4")   # 租户根（=data 上级）之外
        rpt = data / "reports" / "g_full"
        rpt.mkdir(parents=True)
        rows = [{"kind": "down", "src": good},
                {"kind": "down", "src": "http://evil/x.mp4"},
                {"kind": "down", "src": outside}]
        (rpt / "events.jsonl").write_text(
            "\n".join(json.dumps(r) for r in rows), encoding="utf-8")

        real_data, real_env = remix.DATA, os.environ.get("TOOLBOX_DATA_DIR")
        remix.DATA = data
        os.environ["TOOLBOX_DATA_DIR"] = str(data)     # 服务端容器模式
        try:
            pool = remix.discover_pool()
            assert list(pool["videos"]) == [good], pool["videos"]
            assert len(pool["events"]) == 1 and pool["events"][0]["src"] == good
            assert not remix._src_ok(outside)
            assert remix._src_ok(good)
            del os.environ["TOOLBOX_DATA_DIR"]         # 端侧本地模式
            assert remix._src_ok(r"C:\v\a.mp4")
            assert not remix._src_ok("http://a/x.mp4")
        finally:
            remix.DATA = real_data
            if real_env is None:
                os.environ.pop("TOOLBOX_DATA_DIR", None)
            else:
                os.environ["TOOLBOX_DATA_DIR"] = real_env
    print("  remix src 安全闸（URL/租户根夹紧/端侧放行）✓")


def main():
    test_safe_component()
    test_import_session_rejects_traversal()
    test_import_session_src_normalize()
    test_prune_containment()
    test_remix_src_guard()
    print("transfer/remix 安全加固回归 ✓（H-03 / H-05 / M-04）")


if __name__ == "__main__":
    main()
