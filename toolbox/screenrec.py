"""桌面录制：屏幕 + 系统声音 → 按日期的场次目录（1级日期目录 / 2级分P）。

python -m toolbox screenrec [--out <目录>] [--fps 60] [--segment-min 15] [--mic]
（pet 工作台「桌面录制」卡片也走这里，out=录像目录：日期二级目录整体
=队列里的一个任务）

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
- 视频：ffmpeg ddagrab（Desktop Duplication API）全主屏 + libx264。
  ddagrab 是 60 帧的前提：gdigrab 在 2560x1440 实测只采得到 ~34 帧/s
  （时间戳硬凑 60 全靠重复帧，白耗编码）；ddagrab 可跑 57-58 帧。
  启动前探测（抓 1 帧），不可用自动回退 gdigrab 并把帧率降到 30；
  多显示器时 ddagrab 只采 output 0（主屏），gdigrab 采整个虚拟屏。
  编码线程封顶 -threads（默认 8）：x264 默认开 ~1.5x 逻辑核数个帧线程，
  帧线程缓冲是内存大头（1440p 默认 ~30 线程工作集 ~1.3GB，8 线程
  ~0.6GB，实测 CPU 持平、画质不受影响——帧并行不改编码参数）；
- 音频：soundcard WASAPI loopback 录系统声音（可选混录麦克风）→
  s16le 管道喂 ffmpeg；采集/写管道分线程+队列解耦防背压丢帧；
  soundcard 缺失时降级纯视频（事件里提示）；
- 分P轮转：每个分片带 `-t` 时长由 ffmpeg 自行退出（精确、文件必完整）；
  手动停止/暂停用 CTRL_BREAK_EVENT 优雅截断（此路径退出码 255 属正常），
  且停录时音频写线程先关 ffmpeg stdin（EOF）——ffmpeg 主循环阻塞在
  管道读时收不到 CTRL_BREAK 退出标志，不关会拖到超时强杀、mp4 缺 moov；
  等待超时才强杀兜底。

日志：data/logs/screenrec.log（toolbox/logs.py，1MB×3 轮转）。
"""
import re
import signal
import subprocess
import threading
import time
from pathlib import Path

from toolbox import proc
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


DEFAULT_GAME_PROCESSES = ("delta_force", "deltaforce")
# 三角洲行动进程名(小写子串匹配)。
DEFAULT_GAME_EXCLUDE = ("launcher", "updater", "helper", "crashpad")
# **必须排除常驻壳**:2026-10-26 实测本机 delta_force_launcher.exe 在
# 不玩游戏时也常驻(启动器/WeGame 客户端),若只按 delta_force 子串匹配,
# 自动录制会永远判"游戏在跑"、永不停止。排除词按子串命中即忽略该行。


def game_running(names=None, exclude=None, timeout=8, _run=None):
    """目标游戏是否在运行(tasklist CSV 子串匹配 + 排除词)。_run 可注入。

    判定:某行进程名含任一 include 且不含任一 exclude → 在运行。
    用子串而非全名:启动器/客户端/分支进程名跨版本不一致;排除词用来
    剔除 launcher 这类常驻壳(见上)。"""
    pats = [str(n).lower() for n in
            (names if names else DEFAULT_GAME_PROCESSES)]
    pats = [n for n in pats if n]
    if not pats:
        return False
    exc = [str(n).lower() for n in
           (exclude if exclude is not None else DEFAULT_GAME_EXCLUDE)]
    exc = [n for n in exc if n]
    cmd = ["tasklist", "/FO", "CSV", "/NH"]
    run = _run or subprocess.run
    try:
        r = run(cmd, capture_output=True, timeout=timeout,
                creationflags=proc.NO_WINDOW)
    except (OSError, subprocess.TimeoutExpired):
        return False
    out = (r.stdout or b"").decode("gbk", "replace").lower()
    for line in out.splitlines():
        if not any(n in line for n in pats):
            continue
        if any(x in line for x in exc):
            continue                      # 常驻启动器/更新器不算在玩
        return True
    return False


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
        self.cfg = {"fps": 60, "crf": 23, "segment_min": 15, "mic": False,
                    "threads": 8, "capture": "dda"}
        self.cfg.update(cfg or {})
        self.on_event = on_event or (lambda *a: None)
        self._proc = None
        self._thread = None
        self._dda_state = None    # ddagrab 可用性：None=未探测
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
            # 会话停止：主动关音频管道（EOF）。ffmpeg 主循环阻塞在 stdin
            # 读时收不到 CTRL_BREAK 退出标志，不关管道会拖到超时强杀、
            # mp4 缺 moov（2026-10-26 实测）。轮转/暂停不进此分支，管道存活。
            proc = self._proc
            if proc is not None:
                try:
                    proc.stdin.close()
                except (OSError, ValueError):
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

    def _dda_probe(self):
        """ddagrab 可用性探测（抓 1 帧即退），结果缓存。"""
        if self._dda_state is None:
            try:
                r = subprocess.run(
                    ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
                     "-nostdin", "-f", "lavfi", "-i", "ddagrab=0:framerate=30",
                     "-frames:v", "1", "-f", "null", "-"],
                    capture_output=True, timeout=10,
                    creationflags=proc.NO_WINDOW)   # GUI 下探测不闪黑窗
                self._dda_state = (r.returncode == 0)
            except (OSError, subprocess.TimeoutExpired):
                self._dda_state = False
        return self._dda_state

    def _resolve_backend(self):
        """选采集后端：dda（60 帧可达）优先，不可用回退 gdi。

        回退时 gdigrab 高分辨率采不满高帧率（1440p 上限 ~34 帧/s，
        时间戳硬凑全靠重复帧），自动把 fps 压回 30；显式配 capture=gdi
        视为用户知情，不动 fps。
        """
        want = str(self.cfg.get("capture") or "dda").strip().lower()
        if want == "gdi":
            return "gdi"
        if self._dda_probe():
            return "dda"
        self._dda_state = False
        if float(self.cfg["fps"]) > 30:
            self.cfg["fps"] = 30
            self.on_event("error", {"msg": "60 帧采集（Desktop Duplication）"
                                    "不可用，已回退 gdigrab 并降为 30 帧"})
        else:
            self.on_event("error", {"msg": "Desktop Duplication 不可用，"
                                    "已回退 gdigrab"})
        return "gdi"

    def _spawn(self, path: Path, audio: bool, dur: float, backend="dda"):
        """起一个分P的 ffmpeg；-t 到点自行退出（精确轮转、文件完整）。"""
        cmd = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
               "-nostdin"]
        if backend == "dda":
            cmd += ["-f", "lavfi",
                    "-i", f"ddagrab=0:framerate={self.cfg['fps']}"
                          ":draw_mouse=1"]
        else:
            cmd += ["-f", "gdigrab", "-framerate", str(self.cfg["fps"]),
                    "-draw_mouse", "1", "-i", "desktop"]
        if audio:
            cmd += ["-f", "s16le", "-ar", str(RATE), "-ac", "2",
                    "-i", "pipe:0"]
        cmd += ["-map", "0:v"]
        if audio:
            cmd += ["-map", "1:a"]
        if backend == "dda":                 # D3D11 纹理 → 系统内存
            cmd += ["-vf", "hwdownload,format=bgra"]
        cmd += ["-c:v", "libx264", "-preset", "veryfast",
                "-threads", str(int(self.cfg.get("threads") or 8)),
                "-crf", str(self.cfg["crf"]), "-pix_fmt", "yuv420p"]
        if audio:
            cmd += ["-c:a", "aac", "-b:a", "160k"]
        cmd += ["-t", f"{max(1.0, dur):.1f}",
                # 分片 MP4 + 即时刷盘:2026-10-27 实测自动停录时文件尾停在
                # 半个数据包(无 moov,161MB 全损)。视频输入(ddagrab)无界,
                # stdin-EOF 终止不了 ffmpeg,CTRL_BREAK 又会打断包写入——
                # 收尾信号没有可靠优雅路径。分片模式 moov 开头即写好;
                # -flush_packets 1 让每个分片立刻落盘(avio 缓冲否则随进程
                # 一起丢,实测分片了也只有 28 字节头)。任何中断最多丢
                # 最后 ≤2s 的未完整分片,文件始终可播可剪(实测强杀后
                # 6.07s 完整保留)。
                "-movflags", "+frag_keyframe+empty_moov+default_base_moof",
                "-frag_duration", "2",
                "-flush_packets", "1",
                str(path)]
        kw = {"stdout": subprocess.DEVNULL, "stderr": subprocess.PIPE}
        if audio:
            kw.update(stdin=subprocess.PIPE)
        else:
            kw.update(stdin=subprocess.DEVNULL)
        # GUI 进程无控制台,子控制台程序会自开新窗口——必须叠加
        # CREATE_NO_WINDOW(2026-10-26 用户反馈录制闪黑窗);CTRL_BREAK 依赖
        # CREATE_NEW_PROCESS_GROUP,两者可并存(run_pipeline_subprocess 同法)
        creationflags = proc.NO_WINDOW
        if hasattr(subprocess, "CREATE_NEW_PROCESS_GROUP"):
            creationflags |= subprocess.CREATE_NEW_PROCESS_GROUP
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
        """结束当前分P的 ffmpeg(尽量优雅;文件完整性由分片 MP4 兜底)。"""
        p = self._proc
        if not p or p.poll() is not None:
            return
        # 会话停止:先关音频管道(EOF,写线程也可能已关,幂等)——音频流
        # 干净收束;视频输入无界不会自然退,再 BREAK。
        if self._stop_flag.is_set():
            try:
                p.stdin.close()
            except (AttributeError, OSError, ValueError):
                pass
        try:
            if hasattr(signal, "CTRL_BREAK_EVENT"):
                p.send_signal(signal.CTRL_BREAK_EVENT)
            else:
                p.terminate()
        except OSError:
            pass
        try:
            p.wait(timeout=3)
        except subprocess.TimeoutExpired:
            # 写线程未及关管道（异常路径等）：兜底 EOF 解开 stdin 阻塞读
            try:
                p.stdin.close()
            except (AttributeError, OSError, ValueError):
                pass
            try:
                p.wait(timeout=12)
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
        backend = self._resolve_backend()
        LOG.info("采集后端=%s cfg=%s", backend, self.cfg)
        while not self._stop_flag.is_set():
            path = session / f"{title} P{part} {time.strftime('%d日%H点%M分')}.mp4"
            self._proc = self._spawn(path, audio, seg, backend=backend)
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
                    # dda 探测通过但起录即败（驱动/独占全屏等）→ 一次性回退
                    if backend == "dda" and not path.exists():
                        self._dda_state = False
                        if float(self.cfg["fps"]) > 30:
                            self.cfg["fps"] = 30
                        backend = "gdi"
                        if self.files and self.files[-1] == path:
                            self.files.pop()     # 去掉没产出的占位路径
                        self.on_event("error", {"msg": "60 帧采集启动失败，"
                                                "已回退 gdigrab（30 帧）续录"})
                        continue                # 同 P 号重开
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
