# -*- coding: utf-8 -*-
"""桌面录制 60 帧改造回归：ddagrab/gdigrab 后端选择、探测失败自动回退
（fps 压 30 + 事件提示）、ffmpeg 命令构造（-threads 封顶 / -map / -vf）、
CLI 参数合并（缺省不把 None 压进配置——曾致 segment_min=None 崩溃）。
"""
import io
import sys
from pathlib import Path

sys.path.insert(0, ".")

from toolbox import screenrec
from toolbox.cli import _screenrec_cfg, build_parser

ok = 0


def check(cond, what):
    global ok
    assert cond, "FAIL: " + what
    ok += 1
    print("  ✓ " + what)


class _FakeProc:
    """_spawn 不真起 ffmpeg：只吃命令行。"""

    last_cmd = None

    def __init__(self, cmd, **kw):
        self.pid = 4321
        self.cmd = cmd
        self.stderr = io.BytesIO(b"")
        self.stdin = io.BytesIO()
        _FakeProc.last_cmd = self.cmd

    def poll(self):
        return 0


def _rec(cfg=None):
    events = []
    r = screenrec.ScreenRecorder(Path("."), cfg, on_event=lambda k, p: events.append((k, p)))
    return r, events


def main():
    # ---------- 后端选择
    r, ev = _rec()
    r._dda_probe = lambda: True
    check(r._resolve_backend() == "dda", "探测通过 -> dda")

    r, ev = _rec({"fps": 60})
    r._dda_probe = lambda: False
    check(r._resolve_backend() == "gdi", "探测失败 -> 回退 gdi")
    check(r.cfg["fps"] == 30, "回退时 fps 60 自动压 30")
    check(any(k == "error" and "回退" in p["msg"] for k, p in ev),
          "回退发出提示事件")

    r, ev = _rec({"fps": 25, "capture": "dda"})
    r._dda_probe = lambda: False
    r._resolve_backend()
    check(r.cfg["fps"] == 25, "fps<=30 回退不动 fps")

    r, ev = _rec({"fps": 120, "capture": "gdi"})
    check(r._resolve_backend() == "gdi", "显式 capture=gdi 不探测直接 gdi")
    check(r.cfg["fps"] == 120, "显式 gdi 尊重用户 fps（不自动压）")

    # ---------- 命令构造
    orig_popen = screenrec.subprocess.Popen
    screenrec.subprocess.Popen = _FakeProc
    try:
        r, _ = _rec()
        r._spawn(Path("x.mp4"), audio=True, dur=900, backend="dda")
        c = _FakeProc.last_cmd
        check("ddagrab=0:framerate=60:draw_mouse=1" in c and
              "-f" in c and "lavfi" in c, "dda 输入走 lavfi ddagrab")
        check("hwdownload,format=bgra" in c, "dda 带 hwdownload 滤镜")
        check("-threads" in c and c[c.index("-threads") + 1] == "8",
              "x264 线程封顶 -threads 8（默认）")
        check(c.count("-map") == 2 and c[c.index("-map") + 1] == "0:v"
              and c[c.index("-map", c.index("-map") + 1) + 1] == "1:a",
              "双输入显式 -map 0:v -map 1:a")

        r, _ = _rec({"threads": 4})
        r._spawn(Path("x.mp4"), audio=False, dur=900, backend="gdi")
        c = _FakeProc.last_cmd
        check("gdigrab" in c and "-framerate" in c, "gdi 回退走 gdigrab")
        check("hwdownload" not in c, "gdi 不带 hwdownload")
        check(c[c.index("-threads") + 1] == "4", "threads 可配置")
        check(c.count("-map") == 1, "纯视频只 -map 0:v")
    finally:
        screenrec.subprocess.Popen = orig_popen

    # ---------- CLI 合并（缺省不把 None 压进配置）
    p = build_parser()
    a = p.parse_args(["screenrec"])
    cfg = _screenrec_cfg(a)
    check(cfg == {"mic": False}, "CLI 全缺省 -> 只 mic，不覆盖配置文件值")

    a = p.parse_args(["screenrec", "--segment-min", "5", "--fps", "30"])
    cfg = _screenrec_cfg(a)
    check(cfg["segment_min"] == 5.0 and cfg["fps"] == 30,
          "显式 --segment-min/--fps 正常下传")

    # ---------- 默认配置
    from toolbox.config import load_config
    s = load_config().get("screenrec")
    check(s["fps"] == 60 and s["threads"] == 8 and s["capture"] == "dda",
          "默认配置 fps=60/threads=8/capture=dda")



    # ---------- 黑窗修复:NO_WINDOW(用户反馈录制闪命令行窗)
    class _CapPopen(_FakeProc):
        last_flags = None

        def __init__(self, cmd, **kw):
            _CapPopen.last_flags = kw.get("creationflags", 0)
            super().__init__(cmd, **kw)
    screenrec.subprocess.Popen = _CapPopen
    try:
        r, _ = _rec()
        r._spawn(Path("x.mp4"), audio=False, dur=60, backend="gdi")
        import subprocess as _sp2
        check(_CapPopen.last_flags & _sp2.CREATE_NEW_PROCESS_GROUP != 0
              and _CapPopen.last_flags & screenrec.proc.NO_WINDOW != 0,
              "spawn 带 NEW_PROCESS_GROUP|NO_WINDOW(CTRL_BREAK 可用且不闪黑窗)")
    finally:
        screenrec.subprocess.Popen = _FakeProc

    flags_seen = {}
    import subprocess as _sp3

    def _cap_run(*a, **k):
        flags_seen["f"] = k.get("creationflags", 0)
        class _R: returncode = 0; stdout = b""; stderr = b""
        return _R()
    r, _ = _rec()
    r._dda_state = None
    orig = screenrec.subprocess.run
    screenrec.subprocess.run = _cap_run
    try:
        r._dda_probe()
    finally:
        screenrec.subprocess.run = orig
    check(flags_seen.get("f", 0) & screenrec.proc.NO_WINDOW != 0,
          "dda 探测同样带 NO_WINDOW")

    # ---------- 游戏进程探测(自动录制的监控源)
    class _R2: pass

    def _mk_run(out: bytes):
        def run(*a, **k):
            rr = _R2(); rr.stdout = out; rr.returncode = 0
            return rr
        return run
    gr = screenrec.game_running
    check(gr(_run=_mk_run(b'"delta_force_launcher.exe","1234"')) is False,
          "game_running:启动器常驻不算在玩(排除词)")
    check(gr(_run=_mk_run(b'"DeltaForceClient-Win64-Shipping.exe","1"')) is True,
          "game_running:对局客户端算在玩")
    check(gr(["delta_force"], ["launcher"],
             _run=_mk_run(b'"delta_force_launcher.exe","1"')) is False,
          "game_running:排除词可配置")
    check(gr(["delta_force"], [],
             _run=_mk_run(b'"delta_force_launcher.exe","1"')) is True,
          "game_running:排除表置空则该行照常命中(策略可控)")
    check(gr(_run=_mk_run(b'"chrome.exe","1"')) is False,
          "game_running:无关进程不命中")
    check(gr(["xyzgame"], _run=_mk_run(b'"XyzGameClient.exe","9"')) is True,
          "game_running:自定义名子串命中(大小写不敏感)")
    check(gr([], _run=_mk_run(b'"delta_force.exe"')) is True,
          "game_running:空名单回退默认(配置清空不致监控失效)")

    # ---------- 探测缓存（只真探一次）
    import subprocess as _sp
    calls = {"n": 0}

    class _FakeRun:
        def __init__(self, *a, **k):
            calls["n"] += 1
        returncode = 0
        stderr = b""
        stdout = b""
    orig_run = screenrec.subprocess.run
    screenrec.subprocess.run = _FakeRun
    try:
        r, _ = _rec()
        check(r._dda_probe() is True and r._dda_probe() is True
              and calls["n"] == 1, "dda 探测结果缓存(两次调用一次探测)")
        r2, _ = _rec()
        r2._dda_state = False
        check(r2._dda_probe() is False and calls["n"] == 1,
              "已判不可用不再重探")
    finally:
        screenrec.subprocess.run = orig_run

    # ---------- _end_segment 收尾链(BREAK->3s->stdin EOF->12s->强杀)
    class _Stdin:
        def __init__(self):
            self.closed = 0

        def close(self):
            self.closed += 1

    class _HangProc:
        """wait 行为可编排:True=该轮超时(触发 EOF 兜底),其后成功。"""

        def __init__(self, hangs=(False,)):
            self.pid = 777
            self._hangs = list(hangs)
            self.stdin = _Stdin()
            self.killed = 0
            self.signaled = 0
            self.returncode = None

        def poll(self):
            return None

        def send_signal(self, *_):
            self.signaled += 1

        def wait(self, timeout=None):
            hang = self._hangs.pop(0) if self._hangs else False
            if hang:
                raise _sp.TimeoutExpired(cmd="ffmpeg", timeout=timeout)
            self.returncode = 0
            return 0

        def kill(self):
            self.killed += 1
            self.returncode = 1

    r, _ = _rec()
    hp = _HangProc(hangs=[True, False])
    r._proc = hp
    r._end_segment()
    check(hp.signaled == 1 and hp.stdin.closed >= 1 and hp.killed == 0,
          "收尾链:BREAK 后超时->关 stdin(EOF)->正常退出(不强杀)")

    r, _ = _rec()
    hp2 = _HangProc(hangs=[True, True, False])
    r._proc = hp2
    r._end_segment()
    check(hp2.killed == 1 and hp2.stdin.closed >= 1,
          "收尾链:EOF 后仍超时->强杀兜底(不留活进程)")

    r, _ = _rec()
    okp = _HangProc(hangs=[False])
    r._proc = okp
    r._end_segment()
    check(okp.signaled == 1 and okp.killed == 0,
          "收尾链:直接退出不触发兜底")

    r, _ = _rec()
    check(r.stop() == [], "stop 未在录:安静返回空列表")


    print("PASS: " + str(ok) + " 项全部通过")

if __name__ == "__main__":
    main()
