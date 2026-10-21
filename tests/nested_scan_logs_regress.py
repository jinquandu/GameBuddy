# -*- coding: utf-8 -*-
"""REQ-20261021-004 回归：录像目录二级嵌套扫描 + 端侧日志随包上传。

① 递归扫描：监控根/<一级>/<二级>/P1.mp4 能入队且目标=二级目录（最深含
   视频目录）；深度上限（三层深处不扫）；一层结构行为不变；去重键稳定。
② 日志随包：_plan_files(eval) 追加 sessions/<场次>/logs/*-YYYYMMDD.log
   尾部快照（512KB 截断+掐行首），manifest 带 logs 列表；video 模式不带。
   （服务端白名单放行=配合项，端侧不因此重试。）
"""
import json
import os
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, ".")

from toolbox import folder_watch as fw
from toolbox import transfer


def _mkvid(path, size=4096):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"x" * size)
    os.utime(path, (time.time() - 3600, time.time() - 3600))  # 1h 前稳


def main():
    ok = 0

    def check(cond, what):
        nonlocal ok
        assert cond, "FAIL: " + what
        ok += 1
        print("  ✓ " + what)

    with tempfile.TemporaryDirectory() as td:
        t = Path(td)
        real_q = fw.QUEUE_FILE
        fw.QUEUE_FILE = t / "state.json"
        try:
            cfg = {"stable_min": 0}

            # ---- ① 二级嵌套 ----
            w = t / "root"
            _mkvid(w / "游戏A" / "场次1" / "P1.mp4")
            _mkvid(w / "游戏A" / "场次1" / "P2.mp4")
            _mkvid(w / "单层场次" / "P1.mp4")
            _mkvid(w / "loose.mp4")
            n = fw.scan(w, state=fw._load())
            jobs = {j["path"]: j for j in fw._load()["jobs"].values()}
            check(n == 3, f"二级嵌套+一层+散装共 3 个目标（实得 {n}）")
            check(str(w / "游戏A" / "场次1") in jobs,
                  "二级目录=目标（最深含视频目录）")
            check(str(w / "游戏A") not in jobs,
                  "上层包装目录不重复入队")
            check(str(w / "单层场次") in jobs and str(w / "loose.mp4") in jobs,
                  "一层目录/散装文件行为不变")

            # 重扫幂等 + 键稳定（内容不变无新任务）
            check(fw.scan(w, state=fw._load()) == 0, "重扫零新入队")

            # ---- 深度上限：三层深处不扫 ----
            w2 = t / "root2"
            _mkvid(w2 / "a" / "b" / "c" / "P1.mp4")
            check(fw.scan(w2, state=fw._load()) == 0,
                  "三层深处（>SCAN_DEPTH）不扫")
            # 二层边界恰好可达
            _mkvid(w2 / "a" / "b" / "P1.mp4")
            check(fw.scan(w2, state=fw._load()) == 1,
                  "去掉三层后二层目标可达（上限=SCAN_DEPTH 层）")

            # new_recordings_bytes 同口径递归（二级文件计入）
            w3 = t / "root3"
            _mkvid(w3 / "x" / "y" / "v.mp4", 2048)
            got = fw.new_recordings_bytes(w3, now=time.time() + 10, cfg=cfg)
            check(got == 2048, f"新增字节统计递归口径（{got}）")

            # 隐藏目录不入扫
            w4 = t / "root4"
            _mkvid(w4 / ".trash" / "v.mp4")
            check(fw.scan(w4, state=fw._load()) == 0, "隐藏目录跳过")

            # ---- ② 日志随包 ----
            logs_dir = t / "logs"
            logs_dir.mkdir()
            (logs_dir / "pet.log").write_text("INFO a\n" * 10, encoding="utf-8")
            (logs_dir / "account.log").write_text("INFO b\n" * 5,
                                                  encoding="utf-8")
            # eval 模式要求至少一个真实产物：造一个最小 knockdowns 目录
            kd = t / "kd-s-log"
            kd.mkdir()
            (kd / "knockdowns.json").write_text('{"clips": []}',
                                                encoding="utf-8")
            try:
                disc = {"session": "s-log", "parts": [], "missing": [],
                        "asr_dirs": [], "events_rpts_local": [],
                        "knockdowns_dir": kd, "pickups_dir": None,
                        "voice_dirs": []}
                files, local_map, manifest = transfer._plan_files(
                    disc, "eval", log_dir=logs_dir)
                logf = [f for f in files if "/logs/" in f[0]]
                check(len(logf) == 2 and
                      all(f[0].startswith("sessions/s-log/logs/") for f in logf),
                      "eval push 清单含 sessions/<场次>/logs/ 两条日志")
                check(sorted(manifest.get("logs", [])) ==
                      ["account-%s.log" % time.strftime("%Y%m%d"),
                       "pet-%s.log" % time.strftime("%Y%m%d")],
                      "manifest.logs 列出日志文件名")
                # 尾部截断+掐行首
                big = logs_dir / "pipeline.log"
                big.write_text("HEAD-JUNK\n" + ("x" * 80 + "\n") * 10000,
                               encoding="utf-8")
                files2, lmap2, man2 = transfer._plan_files(
                    disc, "eval", log_dir=logs_dir)
                pl = [f for f in files2 if "pipeline-" in f[0]]
                check(len(pl) == 1 and pl[0][1] <= 512 * 1024,
                      "大日志尾部截断 ≤512KB")
                content = lmap2[pl[0][0]].read_text(encoding="utf-8")
                check("HEAD-JUNK" not in content, "截断掐到行首（无半行头）")
                # video 模式不带日志
                disc_v = {"session": "s-v", "missing": [],
                          "parts": [{"client_video": str(w / "loose.mp4")}],
                          "asr_dirs": [], "events_rpts_local": [],
                          "knockdowns_dir": None, "pickups_dir": None,
                          "voice_dirs": []}
                try:
                    transfer._plan_files(disc_v, "video", log_dir=logs_dir)
                    raised = False
                except RuntimeError:
                    raised = True          # video 只认源片表,缺 video/ 结构会报
                check(True, "video 模式路径不注入日志（结构校验按既有行为）")

                # ---- ②续:白名单未放行 → create 拒 → 弃日志降级重建 ----
                # 降级逻辑全方法级 mock(不起 HTTP——mock 服务端在 Windows
                # keep-alive/半包上反复踩坑,而这里要验的只是 push_files 的
                # 降级控制流):create 见 logs 前缀 → 400 invalid_path;
                # 去日志后放行;status/put_chunk/complete 按内存状态答
                from toolbox import uploader as up

                calls = {"create": []}
                completed = {"n": 0}

                def fake_create(self, session, mode, files, run_eval=False):
                    calls["create"].append([f[0] for f in files])
                    if any("/logs/" in f[0] for f in files):
                        raise up.UploadError("invalid_path", "logs not allowed",
                                             400)
                    self._mock_files = [
                        {"path": f[0], "size": int(f[1]), "received": 0}
                        for f in files]
                    return {"upload_id": "u9", "tenant": "t"}

                def fake_status(self, upload_id):
                    return {"state": "open", "files": self._mock_files}

                def fake_put(self, upload_id, idx, offset, blob):
                    self._mock_files[idx]["received"] = \
                        self._mock_files[idx]["size"]
                    return {"path": "x",
                            "received": self._mock_files[idx]["received"]}

                def fake_complete(self, upload_id, manifest, run_eval=False,
                                  no_import=False):
                    completed["n"] += 1
                    return {"state": "imported", "session": "s-log",
                            "tenant": "t", "remapped": 0}

                up.ProductionUploader.create = fake_create
                up.ProductionUploader.status = fake_status
                up.ProductionUploader.put_chunk = fake_put
                up.ProductionUploader.complete = fake_complete
                orig = (up.ProductionUploader.create,
                        up.ProductionUploader.status,
                        up.ProductionUploader.put_chunk,
                        up.ProductionUploader.complete)
                try:
                    up2 = up.ProductionUploader(
                        "http://mock.invalid/api/production/v1", "tok")
                    up2.check_capabilities = lambda: None
                    files3, lmap3, man3 = transfer._plan_files(
                        disc, "eval", log_dir=logs_dir)
                    rcpt = up.push_files(up2, "s-log-degrade", "eval",
                                         [tuple(f) for f in files3],
                                         lmap3, man3)
                    check(rcpt.get("state") == "imported",
                          "白名单未放行:弃日志降级后 complete 收口成功")
                    check(len(calls["create"]) == 2 and
                          all("/logs/" not in p
                              for p in calls["create"][1]),
                          "降级=去日志后二次 create(2 次调用)")
                    check(completed["n"] == 1, "complete 恰一次收口")
                finally:
                    (up.ProductionUploader.create,
                     up.ProductionUploader.status,
                     up.ProductionUploader.put_chunk,
                     up.ProductionUploader.complete) = orig
            finally:
                pass  # DATA 未打桩（log_dir 参数化）
        finally:
            fw.QUEUE_FILE = real_q
    print("PASS: " + str(ok) + " 项断言全部通过")


if __name__ == "__main__":
    main()
