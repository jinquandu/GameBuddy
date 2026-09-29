"""桌面录制：屏幕 + 系统声音 → 按日期的场次目录（1级日期目录 / 2级分P）。

python -m toolbox screenrec [--out <目录>] [--segment-min 15] [--mic]
（pet 卡的「录桌面/暂停/结束」按钮也走这里）

产物结构对齐场次契约，录完可直接 `python -m toolbox pipeline <场次目录>`：
  <out>/<YYYY-MM-DD 桌面录制>/<YYYY-MM-DD 桌面录制> P<k> DD日HH点MM分.mp4
- 1 级 = 日期场次目录；2 级 = 分P视频文件；
- 同日重复录制 P 号续接（扫目录内最大 P + 1）；
- 到 segment_min 分钟轮转下一分P（文件名带该分P起始时刻）。

控制面：
- start() 开始；pause()/resume() 暂停/继续（暂停期间收尾当前分P，
  继续后开新分P，elapsed() 只累计有效录制时间）；
- stop() 结束（优雅写完 mp4 尾）。

技术路线：
- 视频：ffmpeg gdigrab 全桌面 + libx264（系统自带能力，无需装驱动）；
- 音频：soundcard WASAPI loopback 录系统声音（可选混录麦克风）→
  s16le 管道喂 ffmpeg；采集/写管道分线程+队列解耦防背压丢帧；
  soundcard 缺失时降级纯视频（事件里提示）；
- 分P轮转：每个分片带 `-t` 时长由 ffmpeg 自行退出（精确、文件必完整）；
  手动停止/暂停用 CTRL_BREAK_EVENT 优雅截断（此路径退出码 255 属正常），
  等待超时才强杀兜底。

日志：data/logs/screenrec.log（toolbox/logs.py，1MB×3 轮转）。
"""
import re
import signal
import subprocess
import threading
import time
from pathlib import Path

from toolbox.logs import get as _get_log

LOG = _get_log("screenrec")

_P_RE = re.compile(r" P(\d+) ")
RATE = 48000


def _today_title():
    return time.strftime("%Y-%m-%d 桌面录制")


def next_part(session_dir: Path, title: str) -> int:
    """该日期目录下已有的最大 P 号 + 1（没有则 1）。"""
    n = 0
    if session_dir.is_dir():
        for f in session_dir.glob(f"{title} P*.mp4"):
            m = _P_RE.search(f.name)
            if m:
                n = max(n, int(m.group(1)))
    return n + 1


class ScreenRecorder:
    """一次 start()/stop() 生命周期内支持暂停与分P轮转的桌面录制器。

    on_event(kind, payload) 在工作线程里回调（UI 侧自行 marshal）：
      start  {session, path, part, audio}   每个分P开始（含首个/恢复后首个）
      pause  {part, elapsed}                暂停（当前分P已优雅收尾）
      resume {part}                         继续（即将开新分P）
      stop   {session, files, secs}         整次录制结束（secs=有效时长）
      error  {msg}                          降级/异常提示
    """

    def __init__(self, out_root, cfg=None, on_event=None):
        self.root = Path(out_root)
        self.cfg = {"fps": 30, "crf": 23, "segment_min": 15, "mic": False}
        self.cfg.update(cfg or {})
        self.on_event = on_event or (lambda *a: None)
        self._proc = None
        self._thread = None
        self._stop_flag = threading.Event()
        self._paused = threading.Event()
        self._t_start = 0.0
        self._active_t0 = 0.0     # 本段有效录制起点（暂停会重置）
        self._active_acc = 0.0    # 已累计有效录制秒数
        self.files = []

    # ---------- 状态

    @property
    def recording(self):
        return not self._stop_flag.is_set()

    @property
    def paused(self):
        return self._paused.is_set()

    def elapsed(self) -> float:
        """有效录制秒数（不含暂停期间）。"""
        if self._active_t0 <= 0:
            return self._active_acc
        if self.paused or not self.recording:
            return self._active_acc
        return self._active_acc + (time.time() - self._active_t0)

    # ---------- 音频源

    def _open_audio(self):
        """返回 (context, mixer) 或 (None, None)。context 产出 float32 (n,2)。"""
        try:
            import soundcard as sc
        except ImportError:
            LOG.warning("soundcard 未安装，降级纯视频（pip install soundcard）")
            return None, None
        try:
            loop = sc.get_microphone(id=str(sc.default_speaker().name),
                                     include_loopback=True)
            if loop is None:
                LOG.warning("默认扬声器 loopback 不可用，降级纯视频")
                return None, None
            if not self.cfg.get("mic"):
                return loop, None
            mic = sc.default_microphone()
            if mic is None or mic.name == loop.name:
                return loop, None
            LOG.info("混录麦克风：%s", mic.name)
            return loop, mic
        except Exception as e:
            LOG.warning("音频源打开失败（%s），降级纯视频", e)
            return None, None

    def _audio_thread(self, ctx, mixer, proc_ready):
        """录 loopback（可选叠加 mic）→ 队列 → s16le 写 ffmpeg stdin。

        会话级线程：跨分P轮转/暂停存活；无活 ffmpeg 或暂停期间，
        writer 丢弃队列数据（换文件间隙约丢 0.2s，暂停期间整段不采）。
        """
        import numpy as np
        import queue as _q
        import warnings
        warnings.filterwarnings("ignore", message="data discontinuity")
        buf = _q.Queue(maxsize=240)                   # 240×42ms ≈ 10s

        def writer():
            proc_ready.wait(10)
            while not self._stop_flag.is_set() or not buf.empty():
                try:
                    pcm = buf.get(timeout=0.2)
                except _q.Empty:
                    continue
                proc = self._proc
                if (proc is None or proc.poll() is not None
                        or self._paused.is_set()):
                    continue                          # 轮转/暂停期间弃这块
                try:
                    proc.stdin.write(pcm)
                except (BrokenPipeError, ValueError, OSError):
                    pass

        threading.Thread(target=writer, daemon=True).start()
        try:
            with ctx.recorder(samplerate=RATE, blocksize=2048) as r1:
                with mixer.recorder(samplerate=RATE, blocksize=2048) \
                        if mixer else _null_ctx() as r2:
                    while not self._stop_flag.is_set():
                        data = r1.record(numframes=2048)
                        if r2 is not None:
                            try:
                                m = r2.record(numframes=2048)
                                n = min(len(data), len(m))
                                data = data[:n] + m[:n]
                            except Exception:
                                pass
                        pcm = (np.clip(data, -1.0, 1.0) * 32767.0) \
                            .astype("<i2").tobytes()
                        try:
                            buf.put_nowait(pcm)
                        except _q.Full:
                            try:
                                buf.get_nowait()
                            except _q.Empty:
                                pass
                            buf.put_nowait(pcm)
        except Exception as e:
            if not self._stop_flag.is_set():          # 停止时管道撕裂属正常
                LOG.error("音频采集中断：%s", e)
                self.on_event("error", {"msg": f"音频采集中断：{e}"})

    # ---------- ffmpeg

    def _spawn(self, path: Path, audio: bool, dur: float):
        """起一个分P的 ffmpeg；-t 到点自行退出（精确轮转、文件完整）。"""
        cmd = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
               "-nostdin",
               "-f", "gdigrab", "-framerate", str(self.cfg["fps"]),
               "-draw_mouse", "1", "-i", "desktop"]
        if audio:
            cmd += ["-f", "s16le", "-ar", str(RATE), "-ac", "2",
                    "-i", "pipe:0"]
        cmd += ["-c:v", "libx264", "-preset", "veryfast",
                "-crf", str(self.cfg["crf"]), "-pix_fmt", "yuv420p"]
        if audio:
            cmd += ["-c:a", "aac", "-b:a", "160k"]
        cmd += ["-t", f"{max(1.0, dur):.1f}",
                "-movflags", "+faststart", str(path)]
        kw = {"stdout": subprocess.DEVNULL, "stderr": subprocess.PIPE}
        if audio:
            kw.update(stdin=subprocess.PIPE)
        else:
            kw.update(stdin=subprocess.DEVNULL)
        creationflags = 0
        if hasattr(subprocess, "CREATE_NEW_PROCESS_GROUP"):
            creationflags = subprocess.CREATE_NEW_PROCESS_GROUP
        p = subprocess.Popen(cmd, creationflags=creationflags, **kw)
        LOG.info("P 启动 %s（audio=%s -t=%.0fs pid=%d）",
                 path.name, audio, dur, p.pid)
        threading.Thread(target=self._drain_err, args=(p,), daemon=True).start()
        return p

    def _drain_err(self, p):
        try:
            err = p.stderr.read()
            if (err and p.returncode not in (None, 0)
                    and not self._stop_flag.is_set()):
                # CTRL_BREAK 手动截断退出码 255 属正常收尾，只报真错误
                msg = err.decode("utf-8", "replace")[:160]
                LOG.error("ffmpeg stderr：%s", msg)
                self.on_event("error", {"msg": msg})
        except (OSError, ValueError):
            pass

    def _end_segment(self):
        """优雅结束当前 ffmpeg（写完 moov）。"""
        p = self._proc
        if not p or p.poll() is not None:
            return
        try:
            if hasattr(signal, "CTRL_BREAK_EVENT"):
                p.send_signal(signal.CTRL_BREAK_EVENT)
            else:
                p.terminate()
        except OSError:
            pass
        try:
            p.wait(timeout=15)
        except subprocess.TimeoutExpired:
            LOG.warning("ffmpeg 优雅收尾超时，强杀 pid=%d", p.pid)
            p.kill()
            p.wait(timeout=5)
        try:
            p.stdin.close()
        except (AttributeError, OSError, ValueError):
            pass
        LOG.info("分P收尾 pid=%d rc=%s", p.pid, p.returncode)

    # ---------- 生命周期

    def start(self):
        title = _today_title()
        session = self.root / title
        session.mkdir(parents=True, exist_ok=True)
        self.files = []
        self._t_start = time.time()
        self._active_t0 = time.time()
        self._active_acc = 0.0
        self._stop_flag.clear()
        self._paused.clear()
        LOG.info("=== 开场 %s（out=%s cfg=%s）", title, self.root, self.cfg)
        self._thread = threading.Thread(target=self._run,
                                        args=(session, title), daemon=True)
        self._thread.start()

    def pause(self):
        if not self.recording or self.paused:
            return
        el = self.elapsed()
        self._active_acc = el                    # 冻结有效时长
        self._paused.set()
        LOG.info("暂停（P 有效时长 %.1fs）", el)
        self.on_event("pause", {"part": self._cur_part, "elapsed": el})

    def resume(self):
        if not self.recording or not self.paused:
            return
        self._active_t0 = time.time()            # 新一段有效录制
        self._paused.clear()                      # resume 事件由 _run 发（带准 P 号）

    def stop(self, timeout=25):
        """置停止位并等 _run 收尾。返回文件列表。"""
        if not self.recording:
            return self.files
        if self.paused:
            self._active_t0 = 0.0
        else:                                     # 冻结最后一段有效时长
            self._active_acc = self.elapsed()
            self._active_t0 = 0.0
        self._stop_flag.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=timeout)
        LOG.info("=== 收场 files=%d 有效时长=%.1fs", len(self.files),
                 self._active_acc)
        return self.files

    _cur_part = 0

    def _run(self, session, title):
        part = next_part(session, title)
        self._cur_part = part
        ctx, mixer = self._open_audio()
        audio = ctx is not None
        if not audio:
            self.on_event("error", {"msg": "未找到系统声音回采"
                                    "（pip install soundcard），本次纯视频"})
        if audio:                                # 会话级音频线程（跨分P存活）
            ready = threading.Event()
            threading.Thread(target=self._audio_thread,
                             args=(ctx, mixer, ready), daemon=True).start()
            ready.set()
        seg = max(5.0, float(self.cfg["segment_min"]) * 60)
        while not self._stop_flag.is_set():
            path = session / f"{title} P{part} {time.strftime('%d日%H点%M分')}.mp4"
            self._proc = self._spawn(path, audio, seg)
            self.on_event("start", {"session": str(session), "path": str(path),
                                    "part": part, "audio": audio})
            self.files.append(path)
            reason = None                        # stop=结束 / pause=暂停
            while True:
                if self._stop_flag.wait(1.0):
                    reason = "stop"
                    break
                if self._paused.is_set():
                    reason = "pause"
                    break
                if self._proc.poll() is not None:  # -t 到点自然收尾
                    break
            if reason is None:                   # 自然轮转
                rc = self._proc.poll()
                if rc not in (0, None):
                    LOG.error("ffmpeg 异常退出 rc=%d", rc)
                    self.on_event("error", {"msg": f"ffmpeg 异常退出（码 {rc}）"})
                    self.on_event("stop", self._summary(session))
                    return
                part += 1
                self._cur_part = part
                continue
            self._end_segment()                  # 手动停/暂停都优雅截断
            if reason == "stop":
                break
            part += 1                            # 暂停：换新分P等恢复
            self._cur_part = part
            while self._paused.is_set() and not self._stop_flag.wait(0.2):
                pass
            if self._stop_flag.is_set():
                break
            LOG.info("继续 -> P%d", part)
            self.on_event("resume", {"part": part})
        self.on_event("stop", self._summary(session))

    def _summary(self, session):
        ok = [str(f) for f in self.files if f.exists() and f.stat().st_size > 0]
        return {"session": str(session), "files": ok,
                "secs": round(self.elapsed())}


class _null_ctx:
    """mixer 为空时的占位上下文（with 语法对齐）。"""

    def __enter__(self):
        return None

    def __exit__(self, *a):
        return False


def record(out_dir=None, cfg=None):
    """CLI 入口：录到 Ctrl+C（优雅收尾）。返回文件列表。"""
    from toolbox.config import ROOT, load_config
    conf = load_config().get("screenrec") or {}
    conf.update(cfg or {})
    out = Path(out_dir) if out_dir else ROOT / "video"
    box = []

    def on_event(kind, payload):
        if kind == "start":
            print(f"[screenrec] P{payload['part']} -> {payload['path']}"
                  f"（音频：{'系统声音' if payload['audio'] else '无，纯视频'}）",
                  flush=True)
        elif kind == "pause":
            print(f"[screenrec] 已暂停（有效时长 {payload['elapsed']:.0f}s）",
                  flush=True)
        elif kind == "resume":
            print(f"[screenrec] 继续 -> P{payload['part']}", flush=True)
        elif kind == "error":
            print(f"[screenrec][错误] {payload['msg']}", flush=True)
        elif kind == "stop":
            box.append(payload)
            for f in payload["files"]:
                print(f"[screenrec] 已保存 {f}", flush=True)

    rec = ScreenRecorder(out, conf, on_event=on_event)
    try:
        rec.start()
        print(f"[screenrec] 录制中，Ctrl+C 结束 -> {out / _today_title()}",
              flush=True)
        while not box:
            time.sleep(0.5)
    except KeyboardInterrupt:
        print("\n[screenrec] 收尾中…", flush=True)
        rec.stop()
        while not box:
            time.sleep(0.5)
    return box[0]["files"] if box else []
