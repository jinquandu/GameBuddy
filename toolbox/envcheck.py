"""本地环境检测(REQ-20261022-005:替换新手教程)。

登录后跑一次,回答「这台电脑能不能稳定剪片、大约多快」。硬件检测用
demo 素材(39.8MB/720p)做真实解码 + RapidOCR 推理,给出结论;不发布
任何 demo 作品、不跳作品页、不碰服务端生产状态。

检测项(六级):
1. 电脑配置  CPU 核数/内存/系统
2. 磁盘空间  数据盘剩余空间 + 写入速度(录像盘同步看)
3. 视频工具  ffmpeg/ffprobe/libx264(冻结包已捆绑)
4. 体验素材  下载 demo(Range 断点续传 + SHA-256,缓存复用)
5. 视频解码  真实解码 90s 素材,算「几倍实时」
6. 画面识别  RapidOCR 实测 N 帧,算每帧耗时与 30 分钟录像的预计识别时长

结论口径:bad 项会让「环境可用」不成立;warn 仅提示(慢但不阻断)。
检测结果持久化在 data/envcheck/result.json,供 UI 显示「上次检测」。

说明:全帧 OCR 比真实链路(窄条 ROI)更重,预计时长偏保守;演示素材
720p,用户实际 1080p 录像会更慢一些。
"""
import ctypes
import hashlib
import json
import os
import platform
import shutil
import statistics
import subprocess
import sys
import time
import traceback
import urllib.request
from pathlib import Path

import numpy as np

from toolbox import proc
from toolbox.config import DATA, load_config
from toolbox.logs import get as _getlog

LOG = _getlog("envcheck")

# 与旧版新手教程同一份 demo 素材(用户 2026-10-22 指示:硬件检测沿用
# demo 示例),直链在 eval clients 公共分发位,Range 可续传。
DEMO_URL = "http://8.153.157.239/eval/clients/dashijie-tutorial-demo-input.mp4"
DEMO_SHA256 = "c10df0870c7e3489392d4a2c0ab9b3a55f6debb028be900ceab26177982e7e3e"
DEMO_W, DEMO_H = 1280, 720   # 素材实际尺寸(SHA 锁定即常量,第 6 步免 ffprobe)
LEGACY_MATERIAL = DATA / "tutorial" / "demo-input.mp4"   # 旧教程缓存(迁移用)

CHECK_DIR = DATA / "envcheck"
RESULT_FILE = CHECK_DIR / "result.json"
MATERIAL_NAME = "demo-input.mp4"

# 阈值(按演示素材 720p 实测口径;只用于给出提示/结论)
DISK_BAD_GB = 2.0          # 数据盘剩余 < 2GB 直接判不可用
DISK_WARN_GB = 10.0        # < 10GB 提示清理
WRITE_WARN_MBPS = 20.0     # 写入 < 20MB/s 提示磁盘偏慢
RAM_BAD_GB = 2.0
RAM_WARN_GB = 4.0
DECODE_WARN_RATIO = 0.25   # 解码耗时/素材时长 > 0.25 提示偏慢(4×实时以下)
DECODE_BAD_RATIO = 0.60    # < 1.7×实时 基本不可用
OCR_WARN_MS = 800.0        # 每帧 > 0.8s 提示偏慢
OCR_BAD_MS = 2500.0        # 每帧 > 2.5s 判不可用
BENCH_DECODE_SEC = 90      # 解码实测素材长度(秒)
BENCH_OCR_FRAMES = 6       # OCR 实测帧数
WRITE_TEST_BYTES = 16 << 20

LEVEL_GLYPH = {"ok": "✓", "warn": "!", "bad": "✗"}


def _now():
    return time.strftime("%Y-%m-%d %H:%M:%S")


def material_path():
    return CHECK_DIR / MATERIAL_NAME


def _sha256(p):
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for blob in iter(lambda: f.read(1 << 20), b""):
            h.update(blob)
    return h.hexdigest()


def material_ready():
    p = material_path()
    if not p.is_file():
        return False
    try:
        return _sha256(p) == DEMO_SHA256
    except OSError:
        return False


def adopt_legacy_material():
    """旧版本把 demo 缓存在 data/tutorial/:校验通过就迁到 envcheck 目录,
    省一次 40MB 下载(旧教程清理前调用)。"""
    if material_ready():
        return True
    old = LEGACY_MATERIAL
    if not old.is_file():
        return False
    try:
        if _sha256(old) != DEMO_SHA256:
            return False
        CHECK_DIR.mkdir(parents=True, exist_ok=True)
        shutil.copy(old, material_path())
        return True
    except OSError as e:
        LOG.info("旧教程素材迁移跳过:%s", e)
        return False


def download_material(progress=None, chunk=1 << 20):
    """下载 demo 素材(Range 断点续传 + SHA-256 校验)。progress(frac)。
    返回 True=就绪;失败抛异常(调用方转成检测项)。"""
    CHECK_DIR.mkdir(parents=True, exist_ok=True)
    p = material_path()
    if material_ready():
        if progress:
            progress(1.0)
        return True
    tmp = p.with_suffix(".part")
    done = tmp.stat().st_size if tmp.exists() else 0
    req = urllib.request.Request(DEMO_URL)
    if done:
        req.add_header("Range", f"bytes={done}-")
    with urllib.request.urlopen(req, timeout=60) as r:
        total = done + int(r.headers.get("Content-Length") or 0) or None
        with tmp.open("ab") as f:
            while True:
                blob = r.read(chunk)
                if not blob:
                    break
                f.write(blob)
                done += len(blob)
                if progress and total:
                    progress(min(1.0, done / total))
    if _sha256(tmp) != DEMO_SHA256:
        tmp.unlink(missing_ok=True)
        raise RuntimeError("体验素材校验失败，请检查网络后重试")
    tmp.replace(p)
    if progress:
        progress(1.0)
    return True


# ---------------------------------------------------------------- 各项探测

def _total_ram_gb():
    if os.name != "nt":
        return 0.0
    class _MS(ctypes.Structure):
        _fields_ = [("dwLength", ctypes.c_ulong),
                    ("dwMemoryLoad", ctypes.c_ulong),
                    ("ullTotalPhys", ctypes.c_ulonglong),
                    ("ullAvailPhys", ctypes.c_ulonglong),
                    ("ullTotalPageFile", ctypes.c_ulonglong),
                    ("ullAvailPageFile", ctypes.c_ulonglong),
                    ("ullTotalVirtual", ctypes.c_ulonglong),
                    ("ullAvailVirtual", ctypes.c_ulonglong),
                    ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]
    ms = _MS()
    ms.dwLength = ctypes.sizeof(_MS)
    try:
        if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(ms)):
            return 0.0
    except (AttributeError, OSError):
        return 0.0
    return round(ms.ullTotalPhys / (1024 ** 3), 1)


def _cpu_name():
    name = platform.processor() or ""
    if os.name == "nt":
        try:
            import winreg
            with winreg.OpenKey(
                    winreg.HKEY_LOCAL_MACHINE,
                    r"HARDWARE\DESCRIPTION\System\CentralProcessor\0") as k:
                name = str(winreg.QueryValueEx(k, "ProcessorNameString")[0])
        except OSError:
            pass
    return " ".join(name.split())


def system_info():
    return {
        "cpu": os.cpu_count() or 1,
        "cpu_name": _cpu_name(),
        "ram_gb": _total_ram_gb(),
        "os": f"{platform.system()} {platform.release()}",
        "frozen": bool(getattr(sys, "frozen", False)),
    }


def _existing_path(p):
    p = Path(p or DATA)
    while not p.exists() and p.parent != p:
        p = p.parent
    return p


def _disk_free_gb(p):
    try:
        return round(shutil.disk_usage(str(_existing_path(p))).free / 1e9, 1)
    except OSError:
        return None


def disk_info(watch_dir=None):
    """数据盘/录像盘剩余 + 数据盘写入速度实测(16MB)。"""
    out = {"data_free_gb": _disk_free_gb(DATA),
           "watch_free_gb": _disk_free_gb(watch_dir) if watch_dir else None,
           "write_mbps": None, "write_error": ""}
    try:
        CHECK_DIR.mkdir(parents=True, exist_ok=True)
        f = CHECK_DIR / ".write_test"
        blob = b"\0" * (1 << 20)
        t0 = time.perf_counter()
        with open(f, "wb") as fh:
            for _ in range(WRITE_TEST_BYTES >> 20):
                fh.write(blob)
            fh.flush()
            os.fsync(fh.fileno())
        dt = max(1e-6, time.perf_counter() - t0)
        out["write_mbps"] = round((WRITE_TEST_BYTES / 1e6) / dt, 1)
        f.unlink(missing_ok=True)
    except OSError as e:
        out["write_error"] = str(e)
    return out


def _run_version(args, timeout=20):
    r = subprocess.run(args, capture_output=True, text=True, timeout=timeout,
                       creationflags=proc.NO_WINDOW)
    return r.returncode, (r.stdout or "") + (r.stderr or "")


def check_tools():
    """ffmpeg/ffprobe 可执行 + libx264 编码器(切片依赖)。"""
    out = {"ffmpeg": "", "ffprobe": "", "x264": False, "error": ""}
    try:
        rc, txt = _run_version(["ffmpeg", "-version"])
        out["ffmpeg"] = (txt.splitlines() or [""])[0].strip()
        if rc != 0 or not out["ffmpeg"]:
            out["error"] = "ffmpeg 无法运行"
        rc2, txt2 = _run_version(["ffprobe", "-version"])
        out["ffprobe"] = (txt2.splitlines() or [""])[0].strip()
        if rc2 != 0 or not out["ffprobe"]:
            out["error"] = (out["error"] + "；" if out["error"] else "") \
                + "ffprobe 无法运行"
        rc3, txt3 = _run_version(["ffmpeg", "-hide_banner", "-encoders"])
        out["x264"] = rc3 == 0 and "libx264" in txt3
        if not out["x264"]:
            out["error"] = (out["error"] + "；" if out["error"] else "") \
                + "缺少 libx264 编码器"
    except (OSError, subprocess.SubprocessError) as e:
        out["error"] = f"视频工具不可用：{e}"
    return out


def probe_media(path):
    """ffprobe 时长/分辨率(失败抛 RuntimeError)。"""
    from toolbox.ingest import probe_video
    info = probe_video(path)
    if not info.get("duration_sec") or not info.get("width"):
        raise RuntimeError("素材信息读取失败")
    return info


def bench_decode(path, media_sec=BENCH_DECODE_SEC, timeout=180):
    """解码 media_sec 秒素材(丢弃输出),返回耗时/实时倍率。"""
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-ss", "5",
           "-t", str(media_sec), "-i", str(path), "-an",
           "-vf", "fps=1", "-f", "null", "-"]
    t0 = time.perf_counter()
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout,
                       creationflags=proc.NO_WINDOW)
    dt = time.perf_counter() - t0
    if r.returncode != 0:
        raise RuntimeError((r.stderr or "").strip()[:200] or "视频解码失败")
    return {"media_sec": media_sec, "secs": round(dt, 2),
            "ratio": round(dt / media_sec, 3),
            "x_realtime": round(media_sec / dt, 1) if dt > 0 else 0.0}


def _extract_frames(path, n=BENCH_OCR_FRAMES, start=30, timeout=120, size=None):
    """从素材抽 n 帧 BGR 原始帧(1fps)，返回 (frames, w, h)。

    size=(w,h) 为已知尺寸时跳过 ffprobe 预探测——2026-10-28 客户机
    probe 输出被安全软件吞掉导致第 6 步全灭；素材 SHA 锁定，尺寸即常量。"""
    if size:
        w, h = int(size[0]), int(size[1])
    else:
        info = probe_media(path)
        w, h = int(info["width"]), int(info["height"])
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-ss", str(start),
           "-t", str(n), "-i", str(path), "-an", "-vf", "fps=1",
           "-f", "rawvideo", "-pix_fmt", "bgr24", "pipe:1"]
    r = subprocess.run(cmd, capture_output=True, timeout=timeout,
                       creationflags=proc.NO_WINDOW)
    if r.returncode != 0:
        msg = (r.stderr or b"").decode("utf-8", "replace").strip()[:200]
        raise RuntimeError(msg or "抽帧失败")
    frame_bytes = w * h * 3
    buf = r.stdout or b""
    count = len(buf) // frame_bytes
    if count <= 0:
        raise RuntimeError("没有解出视频帧")
    arr = np.frombuffer(buf[:count * frame_bytes], np.uint8)
    frames = arr.reshape(count, h, w, 3)
    return frames, w, h


def bench_ocr(path, n=BENCH_OCR_FRAMES, timeout=120, size=None):
    """真实跑 RapidOCR:与识别链同口径——只 OCR 信息带 SCAN_BAND(画面中央
    窄条),不是全帧;首帧热身不计时,其余取中位耗时。size=(w,h) 已知尺寸
    时透传 _extract_frames 免 ffprobe。"""
    import cv2  # noqa: F401  确保 cv2 先于 onnxruntime 加载(冻结包 DLL 顺序)
    from toolbox.detector import SCAN_BAND, _get_ocr
    frames, w, h = _extract_frames(path, n=n, timeout=timeout, size=size)
    ocr = _get_ocr()
    h, w = frames.shape[1], frames.shape[2]
    x0, y0, x1, y1 = SCAN_BAND
    band = frames[:, int(h * y0):int(h * y1), int(w * x0):int(w * x1)]

    def _one(img):
        res, _ = ocr(img, use_cls=False)
        return res

    _one(band[0])                            # 模型/线程池热身
    times, texts = [], 0
    for frame in band[1:]:
        t0 = time.perf_counter()
        res = _one(frame)
        times.append((time.perf_counter() - t0) * 1000)
        texts += len(res or [])
    ms = statistics.median(times) if times else 0.0
    return {"frames": len(frames), "width": w, "height": h,
            "band": [round(v, 2) for v in SCAN_BAND],
            "ocr_ms": round(ms, 1), "texts": texts,
            "samples_ms": [round(x, 1) for x in times]}


# ---------------------------------------------------------------- 结论

def _item(key, label, level, detail):
    return {"key": key, "label": label, "level": level, "detail": detail}


def _machine_verdict(info):
    cpu, ram = int(info.get("cpu") or 1), float(info.get("ram_gb") or 0)
    detail = f"CPU {cpu} 核"
    if info.get("cpu_name"):
        detail += f"（{info['cpu_name']}）"
    if ram:
        detail += f" · 内存 {ram:g}GB"
    detail += f" · {info.get('os', '')}"
    if ram and ram < RAM_BAD_GB:
        return "bad", detail + "（内存过小，无法稳定运行识别）"
    if cpu < 2 or (ram and ram < RAM_WARN_GB):
        return "warn", detail + "（配置偏低，处理可能较慢）"
    return "ok", detail


def _disk_verdict(info):
    data_gb, watch_gb = info.get("data_free_gb"), info.get("watch_free_gb")
    write = info.get("write_mbps")
    if info.get("write_error"):
        return "bad", "数据目录不可写：" + str(info["write_error"])[:80]
    detail = (f"数据盘可用 {data_gb:g}GB" if data_gb is not None
              else "数据盘空间未知")
    if watch_gb is not None and watch_gb != data_gb:
        detail += f" · 录像盘可用 {watch_gb:g}GB"
    if write:
        detail += f" · 写入 {write:g}MB/s"
    if data_gb is not None and data_gb < DISK_BAD_GB:
        return "bad", detail + "（空间不足，请至少留出 2GB）"
    if (data_gb is not None and data_gb < DISK_WARN_GB) or \
            (watch_gb is not None and watch_gb < DISK_BAD_GB):
        return "warn", detail + "（空间偏紧，建议清理）"
    if write is not None and write < WRITE_WARN_MBPS:
        return "warn", detail + "（磁盘写入偏慢）"
    return "ok", detail


def _tools_verdict(info):
    if info.get("error"):
        return "bad", info["error"]
    ff = (info.get("ffmpeg") or "ffmpeg").split(" Copyright")[0][:40]
    return "ok", f"{ff} · 编码器齐全"


def _material_verdict(ok, err=""):
    """素材取不到只影响「能否实测」,不代表机器不能用(多为网络/服务器
    暂时不可达)——记 warn 并把整体标为「检测未完成」,不给假结论。"""
    return ("ok", "体验素材就绪") if ok else         ("warn", f"体验素材下载失败（{err}），本次未能实测解码与识别")


def _decode_verdict(info):
    ratio = float(info.get("ratio") or 0)
    detail = (f"解码 {info.get('media_sec')} 秒素材用时 "
              f"{info.get('secs')} 秒（约 {info.get('x_realtime')}× 实时）")
    if ratio >= DECODE_BAD_RATIO:
        return "bad", detail + "（解码过慢，无法稳定剪片）"
    if ratio >= DECODE_WARN_RATIO:
        return "warn", detail + "（解码偏慢）"
    return "ok", detail


def _ocr_verdict(info, estimate=None):
    ms = float(info.get("ocr_ms") or 0)
    detail = (f"画面识别 {ms / 1000:.2f} 秒/帧"
              f"（{info.get('width')}×{info.get('height')}，"
              f"实测 {max(0, int(info.get('frames') or 0) - 1)} 帧）")
    if estimate:
        detail += f" · 预计 30 分钟录像约 {estimate:g} 分钟识别"
    if ms >= OCR_BAD_MS:
        return "bad", detail + "（识别过慢，无法稳定剪片）"
    if ms >= OCR_WARN_MS:
        return "warn", detail + "（识别偏慢）"
    return "ok", detail


def _estimate_min_30(info, decode, cpu):
    """按实测速度估算 30 分钟录像的识别耗时(分钟)。

    识别链口径:每秒 1 帧抽帧 OCR + 多进程并行(process 数见
    detector/pickup 的 cpu//4 规则),再叠加解码耗时。"""
    ms = info.get("ocr_ms")
    if not ms:
        return None
    workers = max(2, min(6, max(1, int(cpu or 4)) // 4))
    frames = 1800.0                              # 30 分钟 × 1fps
    ocr_min = frames * (float(ms) / 1000.0) / workers / 60.0
    dec_min = (1800.0 * float((decode or {}).get("ratio") or 0)) / 60.0
    return round(ocr_min + dec_min, 1)


def _save_result(result):
    CHECK_DIR.mkdir(parents=True, exist_ok=True)
    tmp = RESULT_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(result, ensure_ascii=False, indent=1),
                   encoding="utf-8")
    tmp.replace(RESULT_FILE)


def last_result():
    """上次检测结果(dict);没有/损坏返回 {}。"""
    try:
        d = json.loads(RESULT_FILE.read_text(encoding="utf-8"))
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def result_state(result=None):
    """-> off/pass/warn/fail(UI 徽标用)。"""
    r = last_result() if result is None else result
    if not r:
        return "off"
    if not r.get("ok"):
        return "fail"
    return "warn" if r.get("warn") else "pass"


def summary_line(result=None):
    """一行客户可见摘要(设置页卡片)。"""
    r = last_result() if result is None else result
    if not r:
        return "还没检测。登录后会检查这台电脑能不能稳定剪片（约 1-2 分钟）。"
    at = str(r.get("checked_at") or "")[5:16]
    if r.get("incomplete"):
        return (f"上次检测 {at}：未完成（体验素材下载失败），"
                "检查网络后点「重新检测」")
    if not r.get("ok"):
        bad = "、".join(i.get("label", "") for i in r.get("items", [])
                        if i.get("level") == "bad")
        return f"上次检测 {at}：发现问题（{bad}），可点「重新检测」或联系大师姐"
    head = "通过" if not r.get("warn") else f"可用（{r.get('warn')} 项提示）"
    parts = [f"上次检测 {at}：{head}"]
    est = (r.get("estimate") or {}).get("min_per_30")
    if est:
        parts.append(f"预计 30 分钟录像约 {est:g} 分钟识别")
    return " · ".join(parts)


def detail_lines(result=None):
    """检测详情(弹窗用):[(icon, text)]。"""
    r = last_result() if result is None else result
    out = []
    for it in r.get("items", []):
        out.append((LEVEL_GLYPH.get(it.get("level"), "·"),
                    f"{it.get('label')}：{it.get('detail')}"))
    return out


def run(progress=None, watch_dir=None, io=None, user_id=""):
    """执行完整检测并落盘。progress(frac, text);io 供测试注入各探测函数;
    user_id 记入结果,供主窗判断「同账号 7 天内不重复自动检测」。

    返回结果 dict:{ok, warn, checked_at, ts, user_id, machine, bench,
    estimate, items:[{key,label,level,detail}]}
    """
    say = progress or (lambda frac, text: None)
    ext = io or {}
    items = []
    bench = {}

    say(0.02, "[1/6] 检查电脑配置…")
    info = (ext.get("system_info") or system_info)()
    level, detail = _machine_verdict(info)
    items.append(_item("machine", "电脑配置", level, detail))

    say(0.07, "[2/6] 检查磁盘空间与写入速度…")
    disk = (ext.get("disk_info") or disk_info)(watch_dir)
    level, detail = _disk_verdict(disk)
    items.append(_item("disk", "磁盘空间", level, detail))

    say(0.15, "[3/6] 检查视频工具…")
    tools = (ext.get("check_tools") or check_tools)()
    level, detail = _tools_verdict(tools)
    items.append(_item("tools", "视频工具", level, detail))

    say(0.23, "[4/6] 准备体验素材…")
    try:
        dl = ext.get("download_material") or download_material
        okm = dl(progress=lambda f: say(
            0.23 + 0.39 * max(0.0, min(1.0, f)),
            f"[4/6] 下载体验素材 {int(max(0.0, min(1.0, f)) * 100)}%"))
        err = "" if okm else "下载未完成"
    except Exception as e:                     # noqa: BLE001 网络/校验失败入报告
        okm, err = False, str(e)
    level, detail = _material_verdict(bool(okm), err)
    items.append(_item("material", "体验素材", level, detail))
    path = material_path()

    if okm:
        try:
            media = (ext.get("probe_media") or probe_media)(path)
            bench["media"] = media
        except Exception as e:                 # noqa: BLE001
            bench["media"] = {"error": str(e)}
    else:
        bench["media"] = {"error": err}

    say(0.64, "[5/6] 实测视频解码…")
    decode = {}
    if okm:
        try:
            decode = (ext.get("bench_decode") or bench_decode)(path)
            level, detail = _decode_verdict(decode)
            bench.update(decode)
        except Exception as e:                 # noqa: BLE001
            level, detail = "bad", f"视频解码失败：{str(e)[:120]}"
            LOG.warning("视频解码失败:\n%s", traceback.format_exc()[:2000])
    else:
        level, detail = "warn", "素材未就绪，未能实测解码速度"
    items.append(_item("decode", "视频解码", level, detail))

    say(0.80, "[6/6] 实测画面识别…")
    ocr = {}
    est = None
    if okm:
        try:
            ocr = (ext.get("bench_ocr") or bench_ocr)(path,
                                                      size=(DEMO_W, DEMO_H))
            bench.update(ocr)
            est = _estimate_min_30(ocr, decode, info.get("cpu"))
            level, detail = _ocr_verdict(ocr, est)
        except Exception as e:                 # noqa: BLE001
            level, detail = "bad", f"画面识别失败：{str(e)[:120]}"
            LOG.warning("画面识别失败:\n%s", traceback.format_exc()[:2000])
    else:
        level, detail = "warn", "素材未就绪，未能实测画面识别速度"
    items.append(_item("ocr", "画面识别", level, detail))

    bad = sum(1 for i in items if i["level"] == "bad")
    warn = sum(1 for i in items if i["level"] == "warn")
    result = {"ok": bad == 0, "bad": bad, "warn": warn,
              "incomplete": not okm,
              "checked_at": _now(), "ts": time.time(), "user_id": user_id,
              "machine": info, "disk": disk, "tools": tools,
              "bench": bench,
              "estimate": {"min_per_30": est} if est else {},
              "items": items}
    try:
        _save_result(result)
    except OSError as e:
        LOG.warning("检测结果落盘失败:%s", e)
    LOG.info("环境检测完成 ok=%s bad=%d warn=%d bench=%s",
             result["ok"], bad, warn, bench)
    say(1.0, "检测完成")
    return result
