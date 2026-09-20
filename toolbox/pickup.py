"""拾取会话链路：事件粗定位 → 容器界面开合小扫描 → 按开箱会话裁剪。

v2 口径（见 docs/plans/pickup-scoring-plan.md）：评分单元 = **一次开箱/开背包
会话**（容器界面打开→拾取/整理→关闭），不是时间相近的拾取簇，更不是对局。

会话从哪来：事件流的 loot 事件（容器计数增加）只用于**粗定位**「哪里有交互」；
对每个粗簇窗口 [t0-look_back, t1+look_fwd] 对源视频做局部小扫描——裁「自己
容器列」小条（1080p 实测 x∈[0.388,0.456]，右列 0.67+ 是装备对比的对方容器，
刻意裁掉）2fps OCR，恢复：
- 界面开合区间（有容器计数读数的连续帧段，漏读容忍 2 帧）；
- 会话内计数轨迹与跳变（连续确认制，同 detector——观战视角抖动的计数回落丢弃）。

一个开合区间内 ≥1 次计数跳变 = 一个开箱会话片段；截取 [界面开-2s, max(界面关,
末次拾取+6s)]（封顶 max_sec，超长整理会话截断）。mini-scan 全漏时兜底用原簇
直切（保底不丢事件）。

局外剔除（2026-09-20）：大厅整备/仓库整理同样驱动容器计数（口袋/背包/安全箱
在局外 UI 一模一样），粗定位拦不住——实测一字欧/猪猪夏两场直播混入了局后仓
库上架、局前整备片段。判据用大厅菜单栏 OCR（「开始游戏/交易行/改枪台」，对
局内恒为「角色/健康/剩余撤离时间」HUD，两者互斥）；跨会话窗口多帧采样（转
场/出售弹窗会短暂遮蔽菜单，单帧会漏）。注意不能按对局窗口（match_start/
extract）剔——match_start 漏检严重（一字欧 P3/P4/P6/P7 全空但确在对局中，
HUD 撤离计时为证），按窗口剔会误杀真对局片段。

索引携带打分要用的：ui 区间、跳变列表、临近击倒（敌盒语境）、对局归属（abs_t/
match_no，口径同 report.build_matches）。
"""
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import numpy as np

from toolbox.config import DATA, REPORTS, ROOT, load_config
from toolbox.detector import (CONTAINER_RE, PRICE_TABLE, _get_ocr,
                              _video_duration)
from toolbox.knockdown import (_cut, _existing_events, _mmss, _part_no,
                               _slug, collect_videos)
from toolbox.knockdown import events_for as _events_for_kd
from toolbox.report import _part_base, build_matches

NEAR_DOWN_SEC = 25.0    # 段首前该窗口内有击倒视为「击杀后舔包/敌人盒」
# 自己容器列小条（1080p 全帧 OCR 实测标定：口袋/背包/安全箱 计数文字区）
CONTAINER_STRIP = (0.36, 0.48, 0.24, 0.82)   # x0, x1, y0, y1（相对全帧）
UI_GAP_TOL_SEC = 2.2   # 读数空白超过该秒数视为界面闭合（容忍 1-2 帧漏读）
CLIP_MAX_SEC = 90.0    # 单会话片段封顶（超长整理会话截断）
ISLAND_GAP = 30.0      # 粗簇内事件间 >该空档切成事件岛，小扫描逐岛进行
                        # （密集舔包段粗簇可跨 10min+，会话不可能跨 30s 空档）
# 局外大厅菜单栏特征词（对局内恒无）：开始游戏=左下主按钮，交易行/改枪台=设施
# 入口。对局 HUD 的「剩余撤离时间」与之互斥，两主播源实测 2026-09-20
LOBBY_MARKERS = re.compile(r"开始游戏|交易行|改枪台")
LOBBY_MAX_FRAMES = 10   # 会话窗口内均匀采样帧数上限（OCR 成本随帧数线性）


def _islands(cluster):
    """粗簇按事件空档切岛：[{t0, t1, loots}]。"""
    events = sorted(cluster["loots"], key=lambda e: e["t_start"])
    out = []
    for e in events:
        if out and e["t_start"] - out[-1]["t1"] <= ISLAND_GAP:
            out[-1]["t1"] = e["t_start"]
            out[-1]["loots"].append(e)
        else:
            out.append({"t0": e["t_start"], "t1": e["t_start"], "loots": [e]})
    return out


def events_for_pickup(video: Path, config, fps=1.0, workers=1):
    """拾取链路的事件流：已有的该源报告一律复用；没有才现场检测。

    两种复用路径：约定路径 <slug视频名>_full 存在即用——**空文件也是结论**
    （无对局分片此前已付过整段重扫成本，knockdown 的 MIN_EVENTS_REUSE=20
    「过稀重扫」保护在这里只会对其再白扫约 1 小时）；否则按 src 匹配任意
    报告目录（detector 时代旧命名），同样无论多稀都复用。
    """
    conv = REPORTS / (_slug(video.stem) + "_full") / "events.jsonl"
    if conv.exists():
        lines = [l for l in conv.read_text(encoding="utf-8").splitlines()
                 if l.strip()]
        print(f"  {video.name[:36]}：复用报告 {conv.parent.name}"
              f"（{len(lines)} 条）", flush=True)
        return [json.loads(l) for l in lines]
    found = _existing_events(video)
    if found:
        rpt, events = found
        print(f"  {video.name[:36]}：复用事件流 {rpt.parent.name}"
              f"（{len(events)} 条）", flush=True)
        return events
    return _events_for_kd(video, config, fps=fps, workers=workers)


def merge_loots(events, merge_sec):
    """loot 事件按时间排序、相邻合并为粗簇（只用于圈定小扫描窗口）。"""
    loots = sorted((e for e in events if e.get("kind") == "loot"),
                   key=lambda e: e["t_start"])
    segs = []
    for e in loots:
        if segs and e["t_start"] - segs[-1]["t1"] <= merge_sec:
            segs[-1]["t1"] = e["t_start"]
            segs[-1]["loots"].append(e)
        else:
            segs.append({"t1": e["t_start"], "loots": [e]})
    for g in segs:
        g["t0"] = g["loots"][0]["t_start"]
    return segs


# ---------------------------------------------------------------- 界面小扫描

def scan_ui_sessions(path, t_lo, t_hi, fps=1.0):
    """[t_lo, t_hi] 内检测容器界面开合：自己容器列小条 OCR。

    解码与裁剪走 ffmpeg 单遍管道（fps 过滤 + crop，多线程解码远快于 cv2
    逐帧顺序读），OCR 逐帧跑小条。默认 1fps——界面开合边界与跳变确认只需
    1s 粒度（与原事件流一致），窗口成本随 fps 线性。

    返回 sessions: [{ui: [t_open, t_close], reads: [(t, {容器: n})],
                     jumps: [(t, 容器, prev, cur)], net_gain, organizing,
                     fallback: False}]。
    """
    ocr = _get_ocr()
    r = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0",
                        "-show_entries", "stream=width,height",
                        "-of", "csv=p=0", str(path)],
                       capture_output=True, text=True, check=True)
    w, h = (int(x) for x in r.stdout.strip().split(","))
    x0f, x1f, y0f, y1f = CONTAINER_STRIP
    cw, ch = int(w * (x1f - x0f)), int(h * (y1f - y0f))
    p = subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error",
         "-ss", f"{max(0.0, t_lo):.2f}", "-to", f"{t_hi:.2f}", "-i", str(path),
         "-vf", f"fps={fps},crop={cw}:{ch}:{int(w * x0f)}:{int(h * y0f)}",
         "-f", "rawvideo", "-pix_fmt", "bgr24", "-"],
        capture_output=True, check=True)
    buf = np.frombuffer(p.stdout, dtype=np.uint8)
    n = len(buf) // (cw * ch * 3)

    reads = []            # [(t, {容器: n})]
    for i in range(n):
        fr = buf[i * cw * ch * 3:(i + 1) * cw * ch * 3].reshape(ch, cw, 3)
        res, _ = ocr(fr)
        counts = {}
        for box, text, score in (res or []):
            if score < 0.5:
                continue
            m = CONTAINER_RE.search(text)
            if m:
                counts[m.group(1)] = int(m.group(2))
        if counts:
            reads.append((t_lo + (i + 1) / fps, counts))

    hop = 1.0 / fps
    # 切分会话：读数间隔超过容忍窗 = 界面闭合（容忍 1-2 帧漏读）
    sessions = []
    for t, counts in reads:
        if sessions and t - sessions[-1]["t_last"] <= UI_GAP_TOL_SEC:
            s = sessions[-1]
            s["t_last"] = t
            s["reads"].append((t, counts))
        else:
            sessions.append({"t_first": t, "t_last": t, "reads": [(t, counts)]})

    out = []
    for s in sessions:
        # 会话内跳变（连续确认制，同 detector：读数增加后需下一读数保持才记，
        # 计数回落 = 观战视角抖动，丢弃；连续再增视为前次已被证实先记入）
        first_counts = s["reads"][0][1]
        last, pend, jumps = {}, {}, []
        volatile = False          # 出现过计数回落 = 整理/挪动特征（非单调入货）
        for t, counts in s["reads"]:
            for name, cur in counts.items():
                prev = last.get(name)
                if prev is not None and cur < prev:
                    volatile = True
                if prev is not None and cur > prev:
                    if name in pend:
                        pt, pprev, pcur = pend.pop(name)
                        jumps.append((pt, name, pprev, pcur))
                    pend[name] = (t, prev, cur)
                elif name in pend:
                    pt, pprev, pcur = pend.pop(name)
                    if cur >= pcur:
                        jumps.append((pt, name, pprev, pcur))
                    # 回落：观战抖动，丢弃
                last[name] = cur
        last_counts = s["reads"][-1][1]
        net = sum(last_counts.get(n, 0) - first_counts.get(n, 0)
                  for n in set(first_counts) | set(last_counts))
        out.append({"ui": [round(s["t_first"] - hop, 2),
                           round(s["t_last"] + hop, 2)],
                   "reads": s["reads"], "jumps": jumps,
                   "net_gain": net, "organizing": volatile,
                   "fallback": False})
    return out


# ---------------------------------------------------------------- 局外判别

def _lobby_check(video, t0, t1, ocr=None, max_frames=LOBBY_MAX_FRAMES):
    """会话 [t0, t1] 内整帧采样 OCR：出现大厅菜单（开始游戏/交易行/改枪台）
    即判为局外仓库/大厅整理会话。返回 True=局外。

    容器计数（拾取事件）在局外仓库 UI 同样跳变，而大厅菜单栏与对局 HUD
    （剩余撤离时间）互斥，故以此为剔除非对局片段的唯一可靠判据；多帧采样
    是必须的——转场/出售弹窗会短暂遮蔽菜单（实测单帧漏判：交易行上架操作
    的会话恰好在首跳帧落在转场上）。解码走 ffmpeg 单遍 fps 采样管道，
    同 scan_ui_sessions；命即早停，局内会话付满额帧的 OCR 成本。
    """
    ocr = ocr or _get_ocr()
    t0 = max(0.0, t0)
    n = max(2, min(max_frames, int(t1 - t0) + 3))
    fps = n / max(t1 - t0, 1.0)
    p = subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error",
         "-ss", f"{t0:.2f}", "-to", f"{t1:.2f}", "-i", str(video),
         "-vf", f"fps={fps:.4f}", "-f", "rawvideo", "-pix_fmt", "bgr24", "-"],
        capture_output=True, check=True)
    buf = np.frombuffer(p.stdout, dtype=np.uint8)
    if buf.size == 0:
        return False
    r = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0",
                        "-show_entries", "stream=width,height",
                        "-of", "csv=p=0", str(video)],
                       capture_output=True, text=True, check=True)
    w, h = (int(x) for x in r.stdout.strip().split(","))
    per = w * h * 3
    for i in range(len(buf) // per):
        fr = buf[i * per:(i + 1) * per].reshape(h, w, 3)
        res, _ = ocr(fr)
        if res and any(LOBBY_MARKERS.search(text)
                       for _, text, score in res if score > 0.5):
            return True
    return False


# ---------------------------------------------------------------- 会话组装

def scan_islands_parallel(video, todo, cache, cache_path, scan_fps,
                          look_back, look_fwd, workers=5):
    """并行跑缺失岛的小扫描（独立子进程，detector._scan_worker 同套路：
    多进程 OCR 并行是本机唯一有效加速；每 worker 限 2 线程见 _pickup_scan_worker）。

    todo: [(island, ckey)]；完成即增量落盘 cache（中断重跑只补未扫岛）。
    worker 失败的岛记空列表——下游自然走原簇兜底，且不反复重试。
    """
    todo = [(island, ckey) for island, ckey in todo if ckey not in cache]
    if not todo:
        return
    tmpdir = tempfile.mkdtemp(prefix="pk_scan_")
    env = dict(os.environ, PYTHONPATH=str(ROOT))
    n = max(1, min(workers, len(todo)))
    pending = list(todo)
    running = []
    try:
        while pending or running:
            while pending and len(running) < n:
                island, ckey = pending.pop(0)
                out = os.path.join(tmpdir, f"{abs(hash(ckey)) & 0xffffff}.json")
                cmd = [sys.executable, "-m", "toolbox._pickup_scan_worker",
                       str(video),
                       f"{max(0.0, island['t0'] - look_back):.2f}",
                       f"{island['t1'] + look_fwd:.2f}", f"{scan_fps}", out]
                running.append((subprocess.Popen(cmd, env=env), out, ckey, island))
            time.sleep(0.5)
            for item in running[:]:
                proc, out, ckey, island = item
                if proc.poll() is None:
                    continue
                running.remove(item)
                if proc.returncode == 0:
                    with open(out, encoding="utf-8") as f:
                        cache[ckey] = json.load(f)
                else:
                    print(f"    [worker 失败 exit {proc.returncode}] "
                          f"@{island['t0']:.0f}s，该岛走兜底", flush=True)
                    cache[ckey] = []
                cache_path.write_text(json.dumps(cache, ensure_ascii=False),
                                       encoding="utf-8")
                print(f"    [扫描] {len(cache)} 岛完成"
                      f"（{island['t0']:.0f}-{island['t1']:.0f}s）", flush=True)
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def _attach_context(jumps, cluster_events):
    """mini-scan 跳变按 (容器, 时间近邻) 从原事件流附 context/价格表读数。"""
    by_container = {}
    for e in cluster_events:
        m = e.get("meta") or {}
        by_container.setdefault(m.get("container"), []).append(e)

    def ctx_of(name, t):
        cand = by_container.get(name) or []
        best, bd = None, 1e9
        for e in cand:
            d = abs(e["t_start"] - t)
            if d < bd:
                best, bd = e, d
        if best is None or bd > 2.5:
            return {}, None
        meta = best.get("meta") or {}
        return {"context": meta.get("context") or [],
                "voice_hint": meta.get("voice_hint")}, \
            (best.get("price"), best.get("price_source"))

    evs = []
    for t, name, prev, cur in jumps:
        ctx, price = ctx_of(name, t)
        evs.append({"t": round(t, 1), "detail": f"{name} {prev}->{cur}",
                    "container": name, "prev": prev, "cur": cur,
                    "jump": cur - prev, **ctx,
                    "price": price[0] if price else None,
                    "price_source": price[1] if price else None})
    return evs


def _events_from_cluster(cluster):
    """兜底：mini-scan 无果时直接用原簇当会话。"""
    loots = cluster["loots"]
    jumps = []
    for e in loots:
        m = e.get("meta") or {}
        if m.get("cur") is not None and m.get("prev") is not None:
            jumps.append((e["t_start"], m.get("container") or "?",
                          m["prev"], m["cur"]))
    t0 = min(j[0] for j in jumps) if jumps else cluster["t0"]
    t1 = max(j[0] for j in jumps) if jumps else cluster["t1"]
    return {"ui": [t0, t1], "reads": [], "jumps": jumps,
            "net_gain": sum(j[3] - j[2] for j in jumps), "organizing": False,
            "fallback": True}


def _session_label(evlist, price_table):
    """会话标签：价格表命中物品名 > 连拾 > 容器+格数。"""
    for e in evlist:
        texts = list(e.get("context") or []) + list(e.get("voice_hint") or [])
        joined = " ".join(texts)
        for name, _ in sorted(price_table.items(), key=lambda kv: -kv[1]):
            if name in joined:
                return f"拾取_{name}"
    jumps = sum(e["jump"] for e in evlist)
    containers = sorted({e["container"] or "?" for e in evlist})
    if len(evlist) >= 3 or len(containers) > 1:
        return f"连拾x{len(evlist)}"
    return f"拾取_{containers[0]}+{jumps}"


def _bases(videos):
    """各分片基准钟（report 口径：文件名「DD日HH点MM分」），跨零点补一天。"""
    bases, prev = {}, -1
    for v in videos:
        b = _part_base(str(v))
        if b is None:
            b = 0
        if prev >= 0 and b < prev - 12 * 3600:
            b += 86400
        bases[str(v)] = prev = b
    return bases


def _clip_bad(clip: Path) -> bool:
    """解码级完整性自检（ffmpeg -v error 全流读一遍，坏文件会报 NAL 错）。"""
    r = subprocess.run(["ffmpeg", "-v", "error", "-i", str(clip),
                        "-f", "null", "-"], capture_output=True, text=True)
    return r.returncode != 0 or bool(r.stderr.strip())


def _verify_and_recut(out_dir, index, videos, rounds=2):
    """产出完整性自检 + 幂等重切（2026-09-16 实证：一次全量裁剪 16/68 偶发
    写坏——解码可见 NAL 错、时长正常；按 cut 区间重切即愈，故固化为收尾步骤）。"""
    from concurrent.futures import ThreadPoolExecutor
    src_by_name = {v.name: v for v in videos}
    clips = index["clips"]
    for _ in range(rounds):
        with ThreadPoolExecutor(8) as ex:
            bad = [c for c, b in zip(clips, ex.map(
                lambda c: _clip_bad(out_dir / c["file"]), clips)) if b]
        if not bad:
            return
        print(f"  [自检] {len(bad)} 个损坏片段，按 cut 区间重切 ...", flush=True)
        for c in bad:
            src = src_by_name.get(c["src"])
            if src is None:
                print(f"  [自检] 找不到源文件: {c['src']}", flush=True)
                continue
            _cut(src, c["cut"][0], c["cut"][1], out_dir / c["file"])


def pickups(target, config=None, merge=None, redetect=False, fps=1.0,
            workers=1, scan_fps=1.0, scan_workers=None):
    """链路入口：粗定位 → 界面开合小扫描（岛屿级并行）→ 按会话裁剪。"""
    config = config or load_config()
    pk = config.get("pickup", {})
    merge = pk.get("merge_sec", 25.0) if merge is None else merge
    look_back = pk.get("look_back", 35.0)
    look_fwd = pk.get("look_fwd", 15.0)
    max_sec = pk.get("max_sec", CLIP_MAX_SEC)
    scan_workers = pk.get("scan_workers", 5) if scan_workers is None \
        else scan_workers
    pt = dict(PRICE_TABLE)
    pt.update((config.get("detector") or {}).get("price_table", {}))

    videos = collect_videos(target)
    session = (videos[0].parent.name if len(videos) > 1 or Path(target).is_dir()
               else videos[0].stem)
    out_dir = DATA / "pickups" / _slug(session, 60)
    out_dir.mkdir(parents=True, exist_ok=True)   # 小扫描缓存随时回写（可断点续跑）
    index = {"session": session, "clips": [], "matches": []}

    all_events = []
    per_video = []
    for video in videos:
        events = (events_for_pickup(video, config, fps=fps, workers=workers)
                  if not redetect
                  else _events_for_kd(video, config, redetect=True,
                                      fps=fps, workers=workers))
        per_video.append((video, events))
        all_events += events
    bases = _bases(videos)

    matches = build_matches({"videos": [str(v) for v in videos],
                             "events": all_events}) if all_events else []
    enters = [m["enter"] for m in matches]
    index["matches"] = [{k: m[k] for k in
                         ("no", "enter", "dur", "ok", "profit", "downs", "loots")}
                        for m in matches]

    seq = 0
    cache_path = out_dir / "_scancache.json"
    cache = (json.loads(cache_path.read_text(encoding="utf-8"))
             if cache_path.exists() else {})
    lobby_path = out_dir / "_lobbycache.json"
    lobby_cache = (json.loads(lobby_path.read_text(encoding="utf-8"))
                   if lobby_path.exists() else {})
    lobby_skipped = []
    # 本轮全量重切且编号会因局外剔除而变化，先清旧产物（保留 *_cache.json
    # 断点缓存），否则上轮片段残留成无索引孤儿文件
    for old in list(out_dir.glob("*.mp4")):
        old.unlink()
    (out_dir / "pickups.json").unlink(missing_ok=True)
    for video, events in per_video:
        clusters = merge_loots(events, merge)
        downs = sorted(e["t_start"] for e in events if e.get("kind") == "down")
        part = _part_no(video)
        duration = _video_duration(video)
        base = bases[str(video)]
        seen_keys = set()
        n_sessions = 0
        # 岛列表先建齐，未扫描的岛并行跑（断点续跑只补缺口）
        islands_all = []
        for cluster in clusters:
            for island in _islands(cluster):
                ckey = (f"{video.name}|{round(island['t0'], 1)}"
                        f"|{round(island['t1'], 1)}")
                islands_all.append((island, ckey))
        print(f"  {video.name[:36]}：{len(clusters)} 粗簇 -> "
              f"{len(islands_all)} 岛（待扫 "
              f"{sum(1 for _, k in islands_all if k not in cache)}）", flush=True)
        scan_islands_parallel(video, islands_all, cache, cache_path,
                              scan_fps, look_back, look_fwd,
                              workers=scan_workers)
        for island, ckey in islands_all:
            sessions = cache.get(ckey) or []
            usable = [s for s in sessions if s["jumps"]]
            if not usable:
                fb = _events_from_cluster(island)
                if fb["jumps"]:
                    usable = [fb]
            for s in usable:
                key = (str(video), int(s["ui"][0] // 2))
                if key in seen_keys:
                    continue
                seen_keys.add(key)

                evlist = _attach_context(s["jumps"], island["loots"])
                first_pick = min(e["t"] for e in evlist)
                last_pick = max(e["t"] for e in evlist)
                ui0, ui1 = s["ui"]
                label = _session_label(evlist, pt)
                lkey = f"{video.name}|{first_pick:.1f}|{last_pick:.1f}"
                if lkey not in lobby_cache:
                    lobby_cache[lkey] = _lobby_check(
                        video, first_pick - 2.0, last_pick + 1.0)
                    lobby_path.write_text(json.dumps(lobby_cache,
                                                     ensure_ascii=False),
                                          encoding="utf-8")
                if lobby_cache[lkey]:
                    lobby_skipped.append({"part": part,
                                          "t_start": round(first_pick, 1),
                                          "label": label})
                    print(f"    [局外剔除] P{part} {_mmss(first_pick)} {label}"
                          "（大厅/仓库整理，非对局）", flush=True)
                    continue
                n_sessions += 1
                t0 = max(0.0, min(ui0 - 2.0, first_pick - 4.5))
                t1 = min(duration, max(ui1 + 2.5, last_pick + 6.0))
                if t1 - t0 > max_sec:
                    t1 = min(t1, max(t0 + max_sec, last_pick + 4.0))
                seq += 1
                out = out_dir / f"{seq:02d}_P{part}_{_mmss(first_pick)}_{_slug(label, 44)}.mp4"
                _cut(video, t0, t1, out)

                abs_t = base + first_pick
                prev_down = max((d for d in downs if d <= first_pick),
                                default=None)
                match_no = None
                for i, ent in enumerate(enters):
                    if ent <= abs_t:
                        match_no = matches[i]["no"]
                index["clips"].append({
                    "file": out.name, "part": part,
                    "t_start": first_pick, "t_end": last_pick,
                    "cut": [round(t0, 2), round(t1, 2)],
                    "ui": [round(ui0, 1), round(ui1, 1)],
                    "src": video.name, "abs_t": round(abs_t, 1),
                    "match_no": match_no, "fallback": s["fallback"],
                    "jumps": sum(e["jump"] for e in evlist),
                    "net_gain": s.get("net_gain"),
                    "organizing": s.get("organizing", False),
                    "ui_sec": round(ui1 - ui0, 1),
                    "containers": sorted({e["container"] or "?"
                                          for e in evlist}),
                    "near_down": (round(first_pick - prev_down, 1)
                                  if prev_down is not None
                                  and first_pick - prev_down <= NEAR_DOWN_SEC
                                  else None),
                    "events": evlist})
                print(f"    [{seq:02d}] P{part} {_mmss(first_pick)} {label}"
                      f" ui {ui1 - ui0:.0f}s"
                      + ("（兜底）" if s["fallback"] else ""), flush=True)
        print(f"  {video.name[:36]}：{len(clusters)} 粗簇 -> {n_sessions} 会话",
              flush=True)

    out_dir.mkdir(parents=True, exist_ok=True)
    index["lobby_skipped"] = lobby_skipped
    (out_dir / "pickups.json").write_text(
        json.dumps(index, ensure_ascii=False, indent=1), encoding="utf-8")
    _verify_and_recut(out_dir, index, videos)
    print(f"共 {seq} 个拾取会话片段 + pickups.json 已写入 {out_dir}"
          f"（对局 {len(matches)} 个，剔除局外 {len(lobby_skipped)} 个）",
          flush=True)
    return out_dir