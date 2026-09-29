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
match_no，口径同 chrono.build_matches）。
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

import cv2
import numpy as np

from toolbox.chrono import bases_for, build_matches
from toolbox.config import DATA, ROOT, load_config
from toolbox.detector import (CONTAINER_RE, PRICE_TABLE, _get_ocr,
                              _video_duration)
from toolbox.events import (collect_videos, events_for, events_for_pickup,
                            part_no)
from toolbox.knockdown import _cut
from toolbox.naming import mmss as _mmss, slug as _slug

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

def _batch_engine():
    """GPU 批量 OCR 引擎（TOOLBOX_OCR_BATCH=1 的 worker 内启用），失败回退 None。"""
    if not os.environ.get("TOOLBOX_OCR_BATCH"):
        return None
    try:
        from toolbox.detector import _get_batch_ocr
        return _get_batch_ocr()
    except Exception:
        return None


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

    # 窄条小字原生分辨率下 det 噪声框多——rec 逐框跑反而更慢且偶漏读；
    # 2x 放大后 det 候选框少且更准，720p 实测单帧快 30-45%
    frames = [cv2.resize(buf[i * cw * ch * 3:(i + 1) * cw * ch * 3].reshape(ch, cw, 3),
                         None, fx=2, fy=2, interpolation=cv2.INTER_CUBIC)
              for i in range(n)]
    batch = _batch_engine()       # GPU 批量引擎（worker 里启用）；逐帧兜底
    if batch is not None:
        all_res = []
        for i in range(0, len(frames), batch.det_batch):
            all_res.extend(batch.run(frames[i:i + batch.det_batch]))
    else:
        all_res = [ocr(f, use_cls=False)[0] for f in frames]
    reads = []            # [(t, {容器: n})]
    for i, res in enumerate(all_res):
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
    r = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0",
                        "-show_entries", "stream=width,height",
                        "-of", "csv=p=0", str(video)],
                       capture_output=True, text=True, check=True)
    w, h = (int(x) for x in r.stdout.strip().split(","))
    # 只解码顶部菜单带（开始游戏/交易行/改枪台实测 cy≈0.05，2026-09-21）：
    # 整帧 OCR 1280x720 每帧 1-2s，顶部 0-15% 高×70% 宽提速 ~10x
    cw, ch = int(w * 0.70), int(h * 0.15)
    p = subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error",
         "-ss", f"{t0:.2f}", "-to", f"{t1:.2f}", "-i", str(video),
         "-vf", f"fps={fps:.4f},crop={cw}:{ch}:0:0",
         "-f", "rawvideo", "-pix_fmt", "bgr24", "-"],
        capture_output=True, check=True)
    buf = np.frombuffer(p.stdout, dtype=np.uint8)
    if buf.size == 0:
        return False
    per = cw * ch * 3
    for i in range(len(buf) // per):
        fr = buf[i * per:(i + 1) * per].reshape(ch, cw, 3)
        res, _ = ocr(fr, use_cls=False)
        if res and any(LOBBY_MARKERS.search(text)
                       for _, text, score in res if score > 0.5):
            return True
    return False


# ---------------------------------------------------------------- 会话组装

# ---------------------------------------------------------------- 批量并行

def _batch_parallel(worker_module, fixed_args, jobs, n_workers, on_row,
                    gpu_workers=0):
    """通用批量并行：jobs 均衡分组 → 每组一个持久 worker 子进程（模型只加载
    一次，逐任务 flush JSON 行）→ 主进程轮询增量回调 on_row(row)。
    gpu_workers：前 N 个 worker 走 CUDA（不占 CPU 核，混跑聚合吞吐更高）。

    job 为 JSON 可序列化 dict，须含 "key" 与 "cost"（时长秒，贪心均衡用）。
    返回正常产出结果的 key 集合（崩溃未产出的不在其中，调用方按需兜底）。"""
    if not jobs:
        return set()
    tmpdir = tempfile.mkdtemp(prefix="pk_batch_")
    env = dict(os.environ, PYTHONPATH=str(ROOT))
    # 长任务优先装箱（贪心均衡总成本，避免长任务集中拖尾）
    jobs = sorted(jobs, key=lambda j: -(j.get("cost") or 1.0))
    n = max(1, min(n_workers, len(jobs)))
    # GPU worker 吞吐 ~4x：按加权负载装箱，等权会让 CPU 队列成长尾
    wts = [4.0] * min(gpu_workers, n) + [1.0] * max(0, n - gpu_workers)
    queues, load = [[] for _ in range(n)], [0.0] * n
    for j in jobs:
        k = min(range(n), key=lambda i: load[i] / wts[i])
        queues[k].append(j)
        load[k] += j.get("cost") or 1.0
    running = []          # [proc, out_path, parsed_lines]（列表可原地更新）
    done = set()
    try:
        for idx, q in enumerate(queues):
            if not q:
                continue
            jf = os.path.join(tmpdir, f"jobs_{len(running)}.json")
            of = jf.replace("jobs_", "out_")
            Path(jf).write_text(json.dumps(q, ensure_ascii=False),
                                encoding="utf-8")
            cmd = [sys.executable, "-m", worker_module,
                   *[str(a) for a in fixed_args], jf, of]
            env_w = dict(env, TOOLBOX_OCR_CUDA="1") if idx < gpu_workers else env
            running.append([subprocess.Popen(cmd, env=env_w), of, 0])
        while running:
            time.sleep(1.0)
            for item in running[:]:
                proc, of, parsed = item
                if os.path.exists(of):
                    for line in open(of, encoding="utf-8").read().splitlines()[parsed:]:
                        try:
                            row = json.loads(line)
                        except json.JSONDecodeError:
                            break           # 半行（正在写），下轮再读
                        on_row(row)
                        done.add(row.get("key"))
                        parsed += 1
                item[2] = parsed
                if proc.poll() is None:
                    continue
                running.remove(item)
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)
    return done


def scan_islands_parallel(video, todo, cache, cache_path, scan_fps,
                          look_back, look_fwd, workers=None, gpu_workers=None,
                          config=None):
    """并行跑缺失岛的小扫描（批量持久 worker：每进程顺序处理一批岛，OCR
    模型只加载一次；旧版每岛一个进程，31 岛要付 31 次解释器+模型启动）。
    完成即增量落盘 cache（断点续跑只补未扫岛）；worker 失败的岛记空列表
    ——下游自然走原簇兜底，且不反复重试。"""
    from toolbox.detector import _gpu_workers_available
    todo = [(island, ckey) for island, ckey in todo if ckey not in cache]
    if not todo:
        return
    if workers is None:
        workers = max(2, min(5, (os.cpu_count() or 4) // 4))
    if gpu_workers is None:
        # 必须传 config（detector 小节）：传 None 会让配置里调的
        # detector.gpu_workers 在本链路失效（2026-09-22 修复的历史旁路）
        gpu_workers = _gpu_workers_available((config or {}).get("detector"))
    jobs = [{"key": ckey, "kind": "scan",
             "t_lo": round(max(0.0, island["t0"] - look_back), 2),
             "t_hi": round(island["t1"] + look_fwd, 2),
             "it0": island["t0"], "it1": island["t1"],
             "fps": scan_fps,
             "cost": island["t1"] - island["t0"] + look_back + look_fwd}
            for island, ckey in todo]
    want = {j["key"] for j in jobs}

    def on_row(row):
        cache[row["key"]] = row.get("sessions") if row.get("ok") else []
        if not row.get("ok"):
            print(f"    [worker 失败] @{row.get('it0', 0):.0f}s，该岛走兜底",
                  flush=True)
        cache_path.write_text(json.dumps(cache, ensure_ascii=False),
                              encoding="utf-8")
        print(f"    [扫描] {len([k for k in cache if k in want])} 岛完成"
              f"（{row.get('it0', 0):.0f}-{row.get('it1', 0):.0f}s）", flush=True)

    _batch_parallel("toolbox._pickup_scan_batch_worker", [video], jobs,
                    workers, on_row, gpu_workers=gpu_workers)
    for k in want:                     # 崩溃未产出：兜底空，不重试
        if k not in cache:
            cache[k] = []
    cache_path.write_text(json.dumps(cache, ensure_ascii=False),
                          encoding="utf-8")


def lobby_checks_parallel(video, jobs, lobby_cache, lobby_path, workers=None,
                          config=None):
    """局外判别批量并行（jobs: [(lkey, t0, t1)]，整帧 OCR 每帧 ~1-2s，
    串行 27 会话要 ~5-8min）。结果写回 lobby_cache 并逐行落盘。"""
    from toolbox.detector import _gpu_workers_available
    if not jobs:
        return
    if workers is None:
        workers = max(2, min(6, (os.cpu_count() or 4) // 3))
    jbs = [{"key": lkey, "kind": "lobby", "t0": round(t0, 2),
            "t1": round(t1, 2), "cost": t1 - t0}
           for lkey, t0, t1 in jobs]

    def on_row(row):
        lobby_cache[row["key"]] = bool(row.get("lobby"))
        lobby_path.write_text(json.dumps(lobby_cache, ensure_ascii=False),
                              encoding="utf-8")

    _batch_parallel("toolbox._lobby_batch_worker", [video], jbs, workers,
                    on_row,
                    gpu_workers=_gpu_workers_available(
                        (config or {}).get("detector")))


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
            workers=None, scan_fps=1.0, scan_workers=None):
    """链路入口：粗定位 → 界面开合小扫描（岛屿级并行）→ 按会话裁剪。"""
    from concurrent.futures import ThreadPoolExecutor
    config = config or load_config()
    pk = config.get("pickup", {})
    merge = pk.get("merge_sec", 25.0) if merge is None else merge
    look_back = pk.get("look_back", 35.0)
    look_fwd = pk.get("look_fwd", 15.0)
    max_sec = pk.get("max_sec", CLIP_MAX_SEC)
    if scan_workers is None:
        scan_workers = pk.get("scan_workers") \
            or max(2, min(8, (os.cpu_count() or 4) // 2))
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
                  else events_for(video, config, redetect=True,
                                  fps=fps, workers=workers))
        per_video.append((video, events))
        all_events += events
    # abs_t 基准钟：无时间戳分片按 0 计（原 _bases 口径，与 build_matches
    # 内部的 skip 口径不同——后者用于局窗口，前者只做单分片内偏移）
    bases = bases_for(videos, on_missing="zero")

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
        part = part_no(video)
        duration = _video_duration(video)
        base = bases[str(video)]
        seen_keys = set()
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
                              workers=scan_workers, config=config)
        # 候选会话先建齐；局外判别（整帧 OCR，串行一场 5-8min）批量并行
        cands, lobby_jobs = [], []
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
                lkey = f"{video.name}|{first_pick:.1f}|{last_pick:.1f}"
                cands.append({"s": s, "evlist": evlist,
                              "first_pick": first_pick, "last_pick": last_pick,
                              "label": _session_label(evlist, pt), "lkey": lkey})
                if lkey not in lobby_cache:
                    lobby_jobs.append((lkey, first_pick - 2.0, last_pick + 1.0))
        lobby_checks_parallel(video, lobby_jobs, lobby_cache, lobby_path,
                              config=config)

        # 裁剪 + 索引；ffmpeg 重编码切片并行（4 进程 × -threads 4）
        n_sessions = 0
        cut_jobs = []
        for c in cands:
            if lobby_cache.get(c["lkey"]):
                lobby_skipped.append({"part": part,
                                      "t_start": round(c["first_pick"], 1),
                                      "label": c["label"]})
                print(f"    [局外剔除] P{part} {_mmss(c['first_pick'])} "
                      f"{c['label']}（大厅/仓库整理，非对局）", flush=True)
                continue
            n_sessions += 1
            s, evlist = c["s"], c["evlist"]
            ui0, ui1 = s["ui"]
            first_pick, last_pick = c["first_pick"], c["last_pick"]
            t0 = max(0.0, min(ui0 - 2.0, first_pick - 4.5))
            t1 = min(duration, max(ui1 + 2.5, last_pick + 6.0))
            if t1 - t0 > max_sec:
                t1 = min(t1, max(t0 + max_sec, last_pick + 4.0))
            seq += 1
            out = out_dir / (f"{seq:02d}_P{part}_{_mmss(first_pick)}_"
                             f"{_slug(c['label'], 44)}.mp4")
            abs_t = base + first_pick
            prev_down = max((d for d in downs if d <= first_pick),
                            default=None)
            match_no = None
            for i, ent in enumerate(enters):
                if ent <= abs_t:
                    match_no = matches[i]["no"]
            cut_jobs.append({
                "file": out.name, "part": part, "src_video": video,
                "cut": [round(t0, 2), round(t1, 2)],
                "t_start": first_pick, "t_end": last_pick,
                "ui": [round(ui0, 1), round(ui1, 1)],
                "abs_t": round(abs_t, 1), "match_no": match_no,
                "fallback": s["fallback"],
                "jumps": sum(e["jump"] for e in evlist),
                "net_gain": s.get("net_gain"),
                "organizing": s.get("organizing", False),
                "ui_sec": round(ui1 - ui0, 1),
                "containers": sorted({e["container"] or "?" for e in evlist}),
                "near_down": (round(first_pick - prev_down, 1)
                              if prev_down is not None
                              and first_pick - prev_down <= NEAR_DOWN_SEC
                              else None),
                "events": evlist, "print": (f"    [{seq:02d}] P{part} "
                                            f"{_mmss(first_pick)} {c['label']}"
                                            f" ui {ui1 - ui0:.0f}s"
                                            + ("（兜底）" if s["fallback"] else ""))})
        if cut_jobs:
            with ThreadPoolExecutor(4) as ex:
                list(ex.map(lambda j: _cut(j["src_video"], j["cut"][0],
                                           j["cut"][1], out_dir / j["file"]),
                            cut_jobs))
            for j in cut_jobs:
                index["clips"].append(
                    {k: j[k] for k in
                     ("file", "part", "t_start", "t_end", "cut",
                      "ui", "abs_t", "match_no", "fallback", "jumps",
                      "net_gain", "organizing", "ui_sec", "containers",
                      "near_down", "events")}
                    | {"src": j["src_video"].name})
                print(j["print"], flush=True)
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