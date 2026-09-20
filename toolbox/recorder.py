"""B站直播录制（端侧 · 采集节点 0）：直播流直录（ffmpeg -c copy 零重编码）。

与 OBS 屏录的区别：直接从 B站 CDN 拉流转封装，画质 = 直播源画质（主播推
1080P 就是无损 1080P），无采集/编码开销，可无人值守 headless 跑；产物命名
对齐场次契约 `video/<标题>/<标题> P<k> DD日HH点MM分.mp4`（report 的跨分片
基准钟认这个时间戳），录完直接 `python -m toolbox pipeline video/<标题>`。

链路（2026-09-18 对 14735356 房间实测校准）：
- 房间状态/播放流共用 xlive/web-room/v2/index/getRoomPlayInfo（getInfoByRoom
  已被 -352 风控，v2 仅需匿名 buvid3 cookie）；标题取直播页 <title>；
- 匿名指纹 cookie 自动引导：访问主站收割 Set-Cookie 落 .live_anon_cookies.txt
  （Netscape 格式）；bilibili_cookies.txt（登录态）存在时叠加，高画质不受限；
- 画质：按房间 accept_qn 自动取档（请求档不支持时降到最高可用档并提示）；
  qn=10000 即「原画 1080P」（g_qn_desc 新表：15000=2K 20000=4K 30000=杜比）；
- 录制：http_stream + flv + avc 直链，ffmpeg 落 .flv（FLV 对中断天然容忍），
  分片到点优雅停录（stdin 'q'）后 remux 成 mp4(+faststart)，remux 失败保留
  .flv（识别链 collect_videos 原生支持 flv）；
- 看护：文件尺寸看门狗（stall_sec 无增长 = 断流，重拉直链续录新分片）；下播
  自动结束本场，watch 模式继续轮询等开播，P 号延续。

已知代价：分片轮转的 remux 造成数秒空窗（30 分钟一片约丢 2-5s），对识别链
无影响（事件流按分片独立扫描，跨分片只做绝对时间对齐）。

用法：
    python3 -m toolbox record https://live.bilibili.com/14735356     # 守护录制
    python3 -m toolbox record 14735356 --once --segment-min 60       # 只录本场
    python3 -m toolbox record <URL> --probe                          # 只探测画质
"""
import json
import re
import subprocess
import time
import urllib.request
from datetime import datetime
from pathlib import Path

from toolbox.config import ROOT, load_config
from toolbox.download import UA

API = "https://api.live.bilibili.com"
ANON_JAR = ROOT / ".live_anon_cookies.txt"

QN_NAME = {30000: "杜比", 20000: "4K", 15000: "2K", 10000: "原画(1080P)",
           400: "蓝光", 250: "超清", 150: "高清", 80: "流畅"}


# ---------------------------------------------------------------- cookie

def _login_jar_path():
    f = Path(load_config().get("download", {}).get("cookies_file",
                                                   "bilibili_cookies.txt"))
    return f if f.is_absolute() else ROOT / f


def _bootstrap_cookies():
    """匿名指纹 cookie（buvid3 等）：访问主站收割 Set-Cookie，Netscape 落盘缓存。

    v2 取流端点对裸请求回 -352，带上 buvid3 即可通过（2026-09-18 实测）。"""
    req = urllib.request.Request("https://www.bilibili.com/",
                                 headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=15) as r:
        lines = ["# Netscape HTTP Cookie File（toolbox record 自动引导的匿名指纹）"]
        for sc in r.headers.get_all("Set-Cookie") or []:
            first = sc.split(";", 1)[0]
            if "=" not in first:
                continue
            k, v = first.split("=", 1)
            m = re.search(r"domain=([^;]+)", sc, re.I)
            dom = m.group(1).strip() if m else ".bilibili.com"
            lines.append(f"{dom}\tTRUE\t/\tFALSE\t0\t{k.strip()}\t{v.strip()}")
    ANON_JAR.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _cookie_header(refresh=False):
    """登录 cookie（可选）+ 匿名指纹 cookie 合并成请求头。"""
    jar = []
    for f in (_login_jar_path(), ANON_JAR):
        if f == ANON_JAR and (refresh or not f.exists()):
            try:
                _bootstrap_cookies()
            except Exception as e:            # 引导失败不致命：v2 端点有时也放行
                print(f"  [record] 匿名 cookie 引导失败（{type(e).__name__}），继续裸连")
        if not f.exists():
            continue
        for line in f.read_text(encoding="utf-8").splitlines():
            if line and not line.startswith("#") and "\t" in line:
                cols = line.split("\t")
                if len(cols) >= 7:
                    jar.append(f"{cols[5]}={cols[6]}")
    return "; ".join(dict.fromkeys(jar))      # 去重保序


def _http_json(url, refresh=False):
    h = {"User-Agent": UA, "Referer": "https://live.bilibili.com/"}
    ck = _cookie_header(refresh=refresh)
    if ck:
        h["Cookie"] = ck
    req = urllib.request.Request(url, headers=h)
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.loads(r.read())


# ---------------------------------------------------------------- 房间与播放流

def parse_room(target):
    """直播间 URL / 纯房间号 -> 房间号字符串。"""
    s = str(target).strip()
    m = (re.search(r"live\.bilibili\.com/(?:blanc/|h5/)?(\d+)", s)
         or re.search(r"^(\d+)$", s))
    if not m:
        raise ValueError(f"未能从输入解析房间号: {target}（给直播间 URL 或纯数字房间号）")
    return m.group(1)


def room_info(room_id):
    """v2 端点取房间状态（live_status: 0下播 1直播 2轮播），返回 data dict。"""
    api = (f"{API}/xlive/web-room/v2/index/getRoomPlayInfo?room_id={room_id}"
           "&protocol=0,1&format=0,1,2&codec=0,1&platform=web&ptype=16"
           "&dolby=5&panorama=1")
    data = _http_json(api)
    if data.get("code") == -352:              # 指纹过期：刷新匿名 cookie 重试一次
        data = _http_json(api, refresh=True)
    if data.get("code") != 0:
        raise RuntimeError(f"取房间信息失败（code={data.get('code')}）: "
                           f"{data.get('message')}")
    return data["data"]


def room_title(room_id):
    """直播页 <title> 第一段 = 房间标题（失败回退 live_<room_id>）。"""
    try:
        req = urllib.request.Request(f"https://live.bilibili.com/{room_id}",
                                     headers={"User-Agent": UA,
                                              "Cookie": _cookie_header()})
        with urllib.request.urlopen(req, timeout=15) as r:
            html = r.read().decode("utf-8", "ignore")
        m = re.search(r"<title[^>]*>([^<]+)</title>", html)
        if not m:
            return f"live_{room_id}"
        return re.split(r"\s*-\s*哔哩哔哩", m.group(1))[0].strip() or f"live_{room_id}"
    except Exception:
        return f"live_{room_id}"


def _pick_flv_codec(playurl):
    """http_stream + flv + avc 的 codec 节点（直录首选：无 HLS 时延/切片空窗）。"""
    for st in playurl.get("stream") or []:
        if st.get("protocol_name") != "http_stream":
            continue
        for fmt in st.get("format") or []:
            if fmt.get("format_name") != "flv":
                continue
            for codec in fmt.get("codec") or []:
                if codec.get("codec_name") == "avc":
                    return codec
    return None


def get_stream_url(room_id, qn):
    """取播放直链。返回 (url, current_qn, qn_desc, media_info)。

    房间 accept_qn 不含请求档位时，自动降到最高可用档并提示（各房间推流
    上限不同：本链目标是原画 1080P，即主播推流的最高画质）。"""
    api = (f"{API}/xlive/web-room/v2/index/getRoomPlayInfo?room_id={room_id}"
           f"&protocol=0,1&format=0,1,2&codec=0,1&qn={qn}&platform=web"
           f"&ptype=16&dolby=5&panorama=1")
    data = _http_json(api)
    if data.get("code") == -352:
        data = _http_json(api, refresh=True)
    if data.get("code") != 0:
        raise RuntimeError(f"取播放流失败（code={data.get('code')}）: "
                           f"{data.get('message')}")
    d = data["data"]
    if d.get("live_status") != 1:
        raise RuntimeError("房间未开播（live_status="
                           f"{d.get('live_status')}）")
    playurl = (d.get("playurl_info") or {}).get("playurl") or {}
    desc = {q.get("qn"): q.get("desc", "") for q in playurl.get("g_qn_desc") or []}
    codec = _pick_flv_codec(playurl)
    if codec is None:
        raise RuntimeError("无 http_stream/flv 直链（房间可能仅提供 hls）")
    accept = codec.get("accept_qn") or []
    if accept and qn not in accept:
        want = max(accept)
        print(f"  [record] 房间不提供 qn={qn}（{QN_NAME.get(qn, desc.get(qn, ''))}），"
              f"取最高可用 qn={want}（{QN_NAME.get(want, desc.get(want, ''))}）")
        return get_stream_url(room_id, want)
    ui = (codec.get("url_info") or [{}])[0]
    url = ui.get("host", "") + codec.get("base_url", "") + ui.get("extra", "")
    if not url:
        raise RuntimeError("播放流 URL 拼接失败")
    return url, codec.get("current_qn", qn), desc, codec.get("media_info") or {}


# ---------------------------------------------------------------- 命名

def _safe_title(title, limit=60):
    t = re.sub(r"[^\w\u4e00-\u9fff（）()]+", " ", title).strip()
    return (t or "live")[:limit]


def _stamp(dt: datetime):
    """report._part_base 认的跨分片基准钟格式。"""
    return f"{dt.day}日{dt.hour:02d}点{dt.minute:02d}分"


def _next_part(out_root: Path):
    """目录内已有最大 P 号 + 1（断点续录/多次录制 P 号延续）。"""
    mx = 0
    for f in out_root.iterdir():
        m = re.search(r"P(\d+)", f.stem)
        if m:
            mx = max(mx, int(m.group(1)))
    return mx + 1


def _unique_stem(out_root: Path, title, part, started: datetime):
    base = f"{title} P{part} {_stamp(started)}"
    stem, i = base, 2
    while (out_root / f"{stem}.mp4").exists() or (out_root / f"{stem}.flv").exists():
        stem = f"{base}_{i}"
        i += 1
    return stem


# ---------------------------------------------------------------- 录制引擎

def _ffmpeg_cmd(url, flv: Path):
    return ["ffmpeg", "-hide_banner", "-loglevel", "error",
            "-user_agent", UA,
            "-headers", "Referer: https://live.bilibili.com/\r\n",
            "-i", url,
            "-c", "copy", "-f", "flv", "-y", str(flv)]


def _quit_ffmpeg(proc):
    """优雅停录：stdin 发 'q'，ffmpeg 正常收尾（flv 尾部完整）。"""
    try:
        proc.stdin.write(b"q")
        proc.stdin.flush()
    except (OSError, ValueError):
        pass
    try:
        proc.wait(timeout=30)
    except subprocess.TimeoutExpired:
        proc.kill()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            pass
    finally:
        try:
            proc.stdin.close()
        except (OSError, ValueError, AttributeError):
            pass


def _probe_video(flv: Path, min_mb=2, timeout=25):
    """录制中 ffprobe 正在写入的 flv，打印实际分辨率（1080P 要求的确认）。"""
    t0 = time.time()
    while time.time() - t0 < timeout:
        if flv.exists() and flv.stat().st_size > min_mb * 1_000_000:
            try:
                r = subprocess.run(
                    ["ffprobe", "-v", "error", "-select_streams", "v:0",
                     "-show_entries", "stream=width,height,r_frame_rate",
                     "-of", "csv=p=0", str(flv)],
                    capture_output=True, text=True, timeout=15)
                parts = (r.stdout.strip().split(",") + ["0", "0", "0/1"])[:3]
                w, h = int(parts[0] or 0), int(parts[1] or 0)
                num, _, den = parts[2].partition("/")
                fps = float(num) / float(den or 1) if float(den or 1) else 0
                if w and h:
                    tag = "[OK] 1080P 达标" if h >= 1080 else \
                        f"[警告] 低于1080P（{w}x{h}）"
                    print(f"    [流] {w}x{h} @{fps:.0f}fps  {tag}", flush=True)
                return (w, h)
            except Exception:
                return None
        time.sleep(1)
    return None


def _record_segment(url, flv: Path, stop_at, stall_sec):
    """录一个分片。返回 (reason, bytes)。reason ∈ rotate/stop/stall/eof。

    看门狗：文件尺寸 stall_sec 无增长且进程未退出 = 断流（CDN 换节点/主播
    卡顿），kill 后由上层重拉直链续录新分片（被硬杀的 flv 仍是可解码的，
    remux 用 -err_detect ignore_err 抢救）。"""
    print(f"    录制 -> {flv.name}", flush=True)
    proc = subprocess.Popen(_ffmpeg_cmd(url, flv), stdin=subprocess.PIPE)
    seg_t0 = time.time()
    _probe_video(flv)
    last_size, last_growth, last_log = -1, time.time(), seg_t0
    reason = "eof"
    try:
        while True:
            time.sleep(2)
            if proc.poll() is not None:
                reason = "eof"
                break
            size = flv.stat().st_size if flv.exists() else 0
            now = time.time()
            if size != last_size:
                last_size, last_growth = size, now
            elif now - last_growth > stall_sec:
                reason = "stall"
                break
            if now >= stop_at:
                reason = "rotate"
                break
            if now - last_log >= 30:
                last_log = now
                m, s = divmod(int(now - seg_t0), 60)
                print(f"    [REC] {flv.name[:44]} {size / 1e9:.2f}GB {m:02d}:{s:02d}",
                      flush=True)
    except KeyboardInterrupt:
        reason = "stop"
    if reason in ("rotate", "stop"):
        _quit_ffmpeg(proc)
    else:
        proc.kill()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            pass
    return reason, max(0, last_size)


def _remux(flv: Path, mp4: Path):
    """flv -> mp4 转封装（-c copy 秒级完成）。成功删 flv；失败保留 flv 兜底。"""
    if not flv.exists() or flv.stat().st_size == 0:
        flv.unlink(missing_ok=True)
        return None
    cmd = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
           "-err_detect", "ignore_err", "-i", str(flv),
           "-c", "copy", "-bsf:a", "aac_adtstoasc",
           "-movflags", "+faststart", str(mp4)]
    r = None
    for attempt in (1, 2):
        try:
            r = subprocess.run(cmd, capture_output=True, text=True)
            break
        except KeyboardInterrupt:
            if attempt == 2:
                raise
            print("  （Ctrl+C 打断 remux，重试一次收尾；再按一次强退）", flush=True)
    ok = r is not None and r.returncode == 0 and mp4.exists() \
        and mp4.stat().st_size > 10_000
    if not ok:
        mp4.unlink(missing_ok=True)
        print(f"  [警告] remux 失败，保留 {flv.name}（识别链可直接用 flv）: "
              f"{((r.stderr if r else None) or '').strip()[:160]}", flush=True)
        return flv
    flv.unlink(missing_ok=True)
    return mp4


def _record_session(room_id, title, out_root, qn, segment_min, stall_sec):
    """一场直播（开播→下播/停录）的所有分片。返回 (产物列表, 结束原因)。"""
    part = _next_part(out_root)
    outputs, fails = [], 0
    while True:
        try:
            url, cur_qn, desc, media = get_stream_url(room_id, qn)
        except RuntimeError as e:
            # 重试期间房间下播属正常收尾，不算失败
            try:
                if room_info(room_id).get("live_status") != 1:
                    return outputs, "ended"
            except Exception:
                pass
            fails += 1
            if fails >= 8:
                raise RuntimeError(f"连续 {fails} 次取流失败，放弃本场: {e}")
            print(f"  [record] 取流失败（{fails}/8）: {e}，5s 后重试", flush=True)
            time.sleep(5)
            continue
        fails = 0
        started = datetime.now()
        stem = _unique_stem(out_root, title, part, started)
        flv = out_root / f"{stem}.flv"
        print(f"  [record] P{part} {_stamp(started)} 开始"
              f"（qn={cur_qn} {QN_NAME.get(cur_qn, desc.get(cur_qn, ''))}）",
              flush=True)
        reason, nbytes = _record_segment(url, flv,
                                         time.time() + segment_min * 60,
                                         stall_sec)
        out = _remux(flv, out_root / f"{stem}.mp4")
        if out is not None:
            outputs.append(out)
            print(f"  [record] {out.name} 完成（{nbytes / 1e9:.2f}GB，{reason}）",
                  flush=True)
        if reason == "stop":
            return outputs, "stop"
        # 记录太短且几乎无数据 = 取流失败（URL 过期/风控），计入连败
        if nbytes < 1_000_000 and reason in ("eof", "stall"):
            fails += 1
            if fails >= 8:
                raise RuntimeError("连续多段无数据，放弃本场（房间可能已下播）")
        else:
            fails = 0
        if reason == "rotate":
            part += 1
            continue
        # eof / stall：房间还在播才算意外断流，重拉续录；否则本场自然结束
        try:
            live = room_info(room_id).get("live_status")
        except Exception:
            live = 1
        if live != 1:
            return outputs, "ended"
        print("  [record] 流中断但房间仍在播，3s 后重连续录", flush=True)
        time.sleep(3)
        part += 1


# ---------------------------------------------------------------- 入口

def _probe_room(room_id, live, qn):
    if live != 1:
        print("当前未开播，无法探测画质（开播后再试）")
        return
    try:
        url, cur, desc, media = get_stream_url(room_id, qn)
    except RuntimeError as e:
        print(f"探测失败: {e}")
        return
    w, h = media.get("width", "?"), media.get("height", "?")
    print(f"画质：qn={cur} {QN_NAME.get(cur, desc.get(cur, ''))}｜流 {w}x{h}")
    print(f"直链（示例）: {url[:96]}…")


def record(target, once=False, qn=None, out_dir=None, segment_min=None,
           stall_sec=None, poll_sec=None, probe_only=False):
    """链路入口：监控直播间并录制。once=只录本场；默认 watch 守护（下播后
    轮询等开播，P 号延续）。Ctrl+C 优雅收尾当前分片。"""
    cfg = load_config().get("recorder", {})
    qn = int(cfg.get("qn", 10000)) if qn is None else int(qn)
    segment_min = float(cfg.get("segment_min", 30) if segment_min is None
                        else segment_min)
    stall_sec = float(cfg.get("stall_sec", 20) if stall_sec is None else stall_sec)
    poll_sec = float(cfg.get("poll_sec", 30) if poll_sec is None else poll_sec)

    room_id = parse_room(target)
    d = room_info(room_id)
    room_id = str(d.get("room_id") or room_id)
    live = d.get("live_status")
    if not _login_jar_path().exists():
        print("提示：无 bilibili_cookies.txt（匿名模式），个别房间高画质需登录 cookie 才放开")
    print(f"房间 {room_id}｜" + {0: "未开播", 1: "直播中", 2: "轮播中"}.get(
        live, f"状态{live}"))
    if probe_only:
        _probe_room(room_id, live, qn)
        return

    title = _safe_title(room_title(room_id))
    out_root = (Path(out_dir).expanduser() if out_dir else ROOT / "video" / title)
    out_root.mkdir(parents=True, exist_ok=True)
    print(f"输出目录: {out_root}")
    print(f"参数：qn={qn} {QN_NAME.get(qn, '')}｜{segment_min:.0f} 分钟/片｜"
          f"断流 {stall_sec:.0f}s 重连｜"
          + ("--once 单场模式" if once else "watch 守护模式（Ctrl+C 退出）"))

    total = []
    try:
        while True:
            while live != 1:                  # 0 下播 / 2 轮播 都等
                time.sleep(poll_sec)
                try:                          # 单次网络抖动不断守护进程
                    live = room_info(room_id).get("live_status")
                except (RuntimeError, OSError) as e:
                    print(f"  [record] 轮询失败（下轮重试）: {e}", flush=True)
                    continue
                if live == 1:
                    print("[record] 开播了，开始录制", flush=True)
            outputs, why = _record_session(room_id, title, out_root, qn,
                                            segment_min, stall_sec)
            total += outputs
            print(f"[record] 本场结束（{why}）：{len(outputs)} 个分片")
            if once:
                break
            print(f"[record] 继续监控（每 {poll_sec:.0f}s 轮询）", flush=True)
            live = 0
    except KeyboardInterrupt:
        print("\n[record] 收到 Ctrl+C，已收尾退出", flush=True)
    print(f"共 {len(total)} 个分片 -> {out_root}")
    if total:
        print(f"下一步：python -m toolbox pipeline \"{out_root}\"")
    return total
