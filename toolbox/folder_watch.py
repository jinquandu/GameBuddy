"""录像目录监控与自动跑链队列（REQ-20261019-002 D 项首批，2026-10-19）。

用户指定一个监控目录，稳定下来的录像（子目录=场次 / 散装单文件）进入
持久队列，消费者依次跑 `toolbox pipeline <路径>`（末段 push 自动走
HTTPS/rsync 双通道）。设计要点（对应 REQ-002 C/D）：

- **稳定判定**：目标内全部视频文件的 mtime 静置 ≥ stable_min 且非空——
  正在写入的录像不提前处理；
- **去重**：任务键 = sha1(路径|大小|mtime)，同键任何状态都不重复入队；
  文件变化=新任务（旧任务记录保留对账）；
- **持久化**：data/.folder_watch.json（原子写），重启后队列不丢；
- **租约**：running 超过 lease_min 视为宿主崩溃，回队重试（attempts+1），
  达到 attempts_max 记 failed 不再自动重试（人工清记录后可重扫）；
- 上传/生产成功以 pipeline 退出码为准（push 阶段内部有回执校验）。

UI 层（pet）只调 scan/process_pending/状态视图，本模块无 UI 依赖。
"""
import hashlib
import json
import os
import subprocess
import sys
import ctypes
import threading
import time
from pathlib import Path

from toolbox import proc


def _proc_mod():
    """模块别名(局部 proc 变量遮蔽模块名时的取用通道)。"""
    return proc
from toolbox.config import DATA, ROOT, load_config

QUEUE_FILE = DATA / ".folder_watch.json"
VIDEO_EXTS = {".mp4", ".flv", ".ts", ".mkv", ".mov"}
LEGACY_TUTORIAL_SESSION = "新手体验-demo"   # 旧版新手教程沙盒会话名。教程
                                           # 已被环境检测替换
                                           # (REQ-20261022-005),升级时就地
                                           # 清理遗留队列/沙盒
LEGACY_TUTORIAL_DIR = DATA / "tutorial"     # 旧版教程素材缓存(测试可重定向)
# 跑链阶段中文名（与 pipeline.run_pipeline 顺序一致；REQ-20261022-008
# 用于队列行显示「正在做什么」，不靠猜）
PIPELINE_STAGES = [("download", "下载"), ("asr", "转写"),
                   ("speaker", "说话人"), ("detect", "画面识别"),
                   ("knockdowns", "击倒切片"), ("pickups", "拾取切片"),
                   ("queues", "匹配截图"), ("voice", "语音评估"),
                   ("push", "上传")]
# 阶段耗时权重(合计 1.0,口径同 pet.ProgressParser;detect 大头可细化到分片)
STAGE_WEIGHT = {"download": .02, "asr": .03, "speaker": .01, "detect": .75,
                "knockdowns": .10, "pickups": .06, "queues": .01,
                "voice": .01, "push": .01}
STAGE_CN = dict(PIPELINE_STAGES)

_PROGRESS = {}          # session -> {"stage","index","total","ts","started"}
_PROGRESS_LOCK = threading.Lock()

SCAN_DEPTH = 3        # 监控根下递归深度上限（REQ-20261021-004）：目标目录
                      # 最深=监控根/x/y/（二级场次，如 根/<游戏>/<场次>/），
                      # 再深不扫——防误扫用户整盘；只看根直下=旧行为


def _cfg():
    """folder_watch 生效配置:内置默认 < config.yaml < 服务端下发规则
    (REQ-20261026-001 规则2——rules_sync 定时拉取并缓存,服务端最高)。"""
    cfg = dict(load_config().get("folder_watch") or {})
    try:
        from toolbox import rules_sync
        over = rules_sync.applied_overrides()
        if over:
            cfg.update(over)
    except Exception:                          # noqa: BLE001 规则层失败用本地
        pass
    return cfg


def _now():
    return time.strftime("%Y-%m-%d %H:%M:%S")


def _load():
    try:
        state = json.loads(QUEUE_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"jobs": {}}
    _merge_dir_tasks(state)
    return state


def _merge_dir_tasks(state):
    """目录任务聚合迁移(REQ-20261022-002):①同 path 多条(旧键=内容哈希)
    合并为一条;②散装单文件任务(旧版按文件立任务)按父目录聚合为目录
    任务——保留 enqueued_at 最早者(排队序不变),状态优先级
    running>queued>done/failed。幂等。"""
    jobs = state.get("jobs")
    if not isinstance(jobs, dict):
        return
    groups = {}
    for k, j in jobs.items():
        pth = j.get("path", "")
        grp = pth if Path(pth).is_dir() else str(Path(pth).parent)
        groups.setdefault(grp, []).append((k, j))
    prio = {"running": 0, "queued": 1, "done": 2, "failed": 2}
    for grp, items in groups.items():
        if len(items) < 2:
            continue
        items.sort(key=lambda kv: (prio.get(kv[1].get("state"), 3),
                                   kv[1].get("enqueued_at", "")))
        keep_k, keep_j = items[0]
        # 散装组:保留键改为目录键(目录任务口径),session 取目录名
        if not Path(keep_j.get("path", "")).is_dir() and len(items) > 1:
            keep_j["path"] = grp
            keep_j["session"] = Path(grp).name
        for k, _ in items[1:]:
            jobs.pop(k)


def _save(state):
    QUEUE_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = QUEUE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=1),
                   encoding="utf-8")
    tmp.replace(QUEUE_FILE)


def job_key(p: Path):
    """任务键(REQ-20261022-002 优化:一个文件夹=一个任务)。

    目录任务键 = 目录路径哈希——目录内容变化(新分片写入)只更新既有任务
    的键后缀,**不再另立新任务**(旧行为按内部视频清单哈希,同目录会裂成
    多条:mk4 实测裂成 12 条分片任务)。键后缀仍含清单指纹,供状态视图
    显示「内容已变化」。散装文件 = 自身 stat。
    """
    h = hashlib.sha1(str(p).encode("utf-8"))
    if p.is_dir():
        for v in _videos_in(p):
            st = v.stat()
            h.update(f"|{v.name}|{st.st_size}|{int(st.st_mtime)}".encode())
    else:
        st = p.stat()
        h.update(f"|{st.st_size}|{int(st.st_mtime)}".encode())
    return h.hexdigest()


def _videos_in(target: Path, depth=SCAN_DEPTH):
    """目标内视频文件：目录时递归发现（深度上限 depth，REQ-20261021-004
    二级嵌套支持——场次目录可在监控根下一级或二级；0 层 = 仅直下文件，
    与旧行为一致）。"""
    if target.is_file():
        return [target] if target.suffix.lower() in VIDEO_EXTS else []

    def walk(d: Path, level):
        out = []
        try:
            entries = sorted(d.iterdir())
        except OSError:
            return out
        for p in entries:
            try:
                if p.is_file():
                    if p.suffix.lower() in VIDEO_EXTS:
                        out.append(p)
                elif p.is_dir() and level < depth:
                    out.extend(walk(p, level + 1))
            except OSError:
                continue
        return out

    return walk(target, 0)


def _stable(target: Path, stable_sec: int, now=None):
    """目标内全部视频静置 ≥ stable_sec（正在写入的不算稳定）。"""
    vids = _videos_in(target)
    if not vids:
        return False
    now = now or time.time()
    for v in vids:
        try:
            st = v.stat()
        except OSError:
            return False
        if st.st_size == 0 or now - st.st_mtime < stable_sec:
            return False
    return True


def scan(watch_dir, state=None):
    """稳定目标入队（幂等）。返回本次新入队数。

    目标发现递归（REQ-20261021-004）：监控根下走有界深度优先，「目标」=
    含视频的最深目录（场次目录可在一/二级）或根直下散装视频；一个子树
    只出一个目标（以最深含视频目录为准），上层包装目录不重复入队。"""
    wd = Path(watch_dir)
    if not wd.is_dir():
        return 0
    state = state if state is not None else _load()
    cfg = _cfg()
    stable_sec = int(cfg.get("stable_min", 10)) * 60
    new = 0

    def try_enqueue(target):
        nonlocal new
        try:
            key = job_key(target)
        except OSError:
            return
        if key in state["jobs"]:
            return                         # 去重：任何状态都不重复入队
        # 目录任务聚合(REQ-20261022-002):一个文件夹=一个任务。同目录的
        # 旧任务(旧键=内容清单哈希,内容变化会裂成多条)→ 沿用旧键/状态/
        # 排位,只更新内容指纹说明;done/failed 的目录内容更新 → 重新排队
        # (新素材该处理);queued/running 不动(已在等/在跑)。
        target_dir = str(target)
        stale = [k for k, j in state["jobs"].items()
                 if j["path"] == target_dir and k != key]
        if stale:
            keep = stale[0]
            for k in stale[1:]:
                state["jobs"].pop(k)
            old = state["jobs"][keep]
            old["note"] = "内容已更新"
            old["updated_at"] = _now()
            if old["state"] in ("queued", "running"):
                return                     # 已在等/在跑:不重复入队
            old["state"] = "queued"        # done/failed+新内容 → 重新排队
            old["attempts"] = 0
            return
        # 散装文件已被父目录任务聚合过(_merge_dir_tasks 之后)→ 不再按
        # 文件另立任务(否则每次扫描重演「入队 N 个→下次又聚合回 1 个」,
        # 2026-10-26 排障发现日志每 2h 刷「扫描入队 11 个新目标」即此)
        if target.is_file():
            parent_dir = str(target.parent)
            if any(j.get("path") == parent_dir
                   for j in state["jobs"].values()):
                return
        state["jobs"][key] = {
            "key": key, "path": str(target),
            "session": target.stem if target.is_file() else target.name,
            "state": "queued", "attempts": 0,
            "enqueued_at": _now(), "updated_at": _now(), "note": ""}
        new += 1

    def walk(entry, level):
        """返回 True=本子树已产出/包含目标（上层不再处理）。
        level: 根直下条目=0;目标（场次目录）最深=SCAN_DEPTH-1 层
        （SCAN_DEPTH=3 → 根/a/b/ 二级嵌套）；散装文件限根直下。"""
        if entry.is_file():
            if level == 0 and entry.suffix.lower() in VIDEO_EXTS and \
                    _stable(entry, stable_sec):
                try_enqueue(entry)
                return True
            return False
        if not entry.is_dir():
            return False
        # 先探更深一层：子树里能找到目标则本目录只是包装层
        if level < SCAN_DEPTH - 1:
            try:
                subs = sorted(p for p in entry.iterdir()
                              if p.is_dir() and not p.name.startswith("."))
            except OSError:
                subs = []
            handled = any(walk(s, level + 1) for s in subs)
            if handled:
                return True
        if level >= SCAN_DEPTH - 1:
            return False                    # 超出最深目标层（防误扫整盘）
        vids = _videos_in(entry, depth=0)   # 只看本目录直下文件
        if vids and _stable(entry, stable_sec):
            try_enqueue(entry)
            return True
        return False

    try:
        roots = sorted(wd.iterdir())
    except OSError:
        return 0
    for entry in roots:
        if entry.name.startswith("."):
            continue
        walk(entry, 0)                     # 根直下散装视频/一二级目录
    # new>0 或发生目录任务聚合(note/键更新)都要落盘;同时记扫描时刻(UI)
    prev_ts = state.get("last_scan_ts")
    state["last_scan_ts"] = time.time()
    if new or any(j.get("note") == "内容已更新"
                  for j in state["jobs"].values()) or prev_ts is None:
        _save(state)
    return new


def expire_leases(state=None):
    """崩溃租约回收：running 超时 → queued（attempts+1，超限记 failed）。"""
    state = state if state is not None else _load()
    cfg = _cfg()
    lease_sec = int(cfg.get("lease_min", 120)) * 60
    amax = int(cfg.get("attempts_max", 5))
    changed = False
    for j in state["jobs"].values():
        if j["state"] != "running":
            continue
        # updated_at 是本地时间串；租约用 enqueued 后的墙钟不可靠，
        # running 任务带 taken_ts（epoch）
        taken = j.get("taken_ts", 0)
        if taken and time.time() - taken > lease_sec:
            j["attempts"] += 1
            j["state"] = "failed" if j["attempts"] >= amax else "queued"
            j["note"] = f"租约超时回收（第{j['attempts']}次）"
            j["updated_at"] = _now()
            j.pop("taken_ts", None)
            changed = True
    if changed:
        _save(state)
    return changed


def claim_next(state=None):
    """领取一个 queued 任务 → running（记租约）。无任务返回 None。"""
    state = state if state is not None else _load()
    for j in sorted(state["jobs"].values(), key=lambda x: x["enqueued_at"]):
        if j["state"] == "queued":
            j["state"] = "running"
            j["attempts"] += 1
            j["taken_ts"] = time.time()
            j["updated_at"] = _now()
            _save(state)
            return j
    return None


def finish(key, ok, note="", state=None):
    state = state if state is not None else _load()
    j = state["jobs"].get(key)
    if not j:
        return
    amax = int(_cfg().get("attempts_max", 5))
    j.pop("taken_ts", None)
    if ok:
        j["state"], j["note"] = "done", note
        # 记录「按哪份内容处理完的」(REQ-20261026-001):目录内容后续再变,
        # 当前指纹 != done_key → 重新计入未处理;旧任务无此键则回退比 key。
        try:
            j["done_key"] = job_key(Path(j.get("path", "")))
        except OSError:
            pass
    elif j["attempts"] >= amax:
        j["state"], j["note"] = "failed", note
    else:
        j["state"], j["note"] = "queued", note
    j["updated_at"] = _now()
    _save(state)


def _kill_tree(proc):
    """Windows 树杀（与 pet 同法）：taskkill /T 防孙进程孤儿。"""
    if proc is None or proc.poll() is not None:
        return
    if os.name == "nt":
        subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                       capture_output=True, creationflags=proc.NO_WINDOW)
    else:
        proc.kill()


_CURRENT = None        # 当前跑链子进程（pet 退出时 kill_current 防孤儿）


def kill_current():
    global _CURRENT
    _kill_tree(_CURRENT)
    _CURRENT = None


def _progress_begin(session):
    with _PROGRESS_LOCK:
        _PROGRESS[session] = {"stage": "准备", "index": 0,
                             "total": len(PIPELINE_STAGES),
                             "ts": time.time(), "started": time.time(),
                             "done": set(), "det_done": 0, "kd_done": 0}


def _progress_frac(info):
    """按阶段权重算整体进度(0..1)。跳过=完成;detect/knockdowns 细化到
    分片(检测完成/粗簇行);其余运行中阶段算半程。供 UI 显示%与预计时间。"""
    done = set(info.get("done") or ())
    frac = sum(STAGE_WEIGHT.get(k, 0) for k in done)
    parts = int(info.get("parts") or 0)
    cur = info.get("cur") or ""
    if cur == "detect" and parts:
        frac += STAGE_WEIGHT["detect"] * min(1.0, info.get("det_done", 0)
                                             / parts)
    elif cur == "knockdowns" and parts:
        frac += STAGE_WEIGHT["knockdowns"] * min(1.0, info.get("kd_done", 0)
                                                 / parts)
    elif cur:
        frac += STAGE_WEIGHT.get(cur, 0) * 0.5
    return min(frac, 0.99)


def _progress_view(info):
    """-> {stage,frac,eta_min}(eta 按已用时间外推;样本不足为 None)。"""
    frac = _progress_frac(info)
    eta = None
    if frac >= 0.05:
        el = time.time() - float(info.get("started") or time.time())
        eta = max(0.1, el * (1 - frac) / frac / 60.0)
    return {"stage": info.get("stage", ""), "frac": round(frac, 3),
            "eta_min": round(eta, 1) if eta is not None else None}


def _progress_line(session, line):
    """从 pipeline stdout 提取阶段推进;认不出的行忽略。"""
    import re as _re
    body = line.strip()
    if body.startswith("[pipeline]"):
        body = body[len("[pipeline]"):].strip()
    m = _re.match(r"^-- 跳过 (\w+)$", body)
    if m and m.group(1) in STAGE_WEIGHT:
        with _PROGRESS_LOCK:
            cur = _PROGRESS.setdefault(session, {})
            cur.setdefault("done", set()).add(m.group(1))
        return
    m = _re.match(r"^(\w+) 开始", body)
    if m and m.group(1) in STAGE_CN:
        key = m.group(1)
        idx = [k for k, _ in PIPELINE_STAGES].index(key) + 1
        with _PROGRESS_LOCK:
            cur = _PROGRESS.setdefault(session, {})
            cur.update(stage=STAGE_CN[key], index=idx, ts=time.time(),
                       cur=key)
            cur.setdefault("done", set()).discard(key)
        return
    m = _re.match(r"^(\w+) 完成", body)
    if m and m.group(1) in STAGE_CN:
        key = m.group(1)
        idx = [k for k, _ in PIPELINE_STAGES].index(key) + 1
        with _PROGRESS_LOCK:
            cur = _PROGRESS.setdefault(session, {})
            cur.update(stage=STAGE_CN[key] + "完成", index=idx,
                       ts=time.time(), cur="")
            cur.setdefault("done", set()).add(key)
        return
    m = _re.match(r"场次：(.+?)，(\d+) 个分片", body)
    if m:
        with _PROGRESS_LOCK:
            _PROGRESS.setdefault(session, {}).update(
                parts=int(m.group(2)), ts=time.time())
        return
    m = _re.match(r"^(.{1,64}?\.mp4)：(检测完成|复用事件流)", body)
    if m:
        with _PROGRESS_LOCK:
            cur = _PROGRESS.setdefault(session, {})
            seen = cur.setdefault("det_set", set())
            seen.add(m.group(1)[:36])
            cur["det_done"] = len(seen)
            cur["ts"] = time.time()
        return
    m = _re.match(r"^(.{1,64}?\.mp4)：\d+ 粗簇", body)
    if m:
        with _PROGRESS_LOCK:
            cur = _PROGRESS.setdefault(session, {})
            seen = cur.setdefault("kd_set", set())
            seen.add(m.group(1)[:36])
            cur["kd_done"] = len(seen)
            cur["ts"] = time.time()
        return


def _progress_end(session):
    with _PROGRESS_LOCK:
        cur = _PROGRESS.get(session)
        if cur is not None:
            cur["finished_ts"] = time.time()
            cur["stage"] = "结束"


def scan_stats(watch_dir):
    """目录扫描摘要(UI「最近检查/发现视频/待处理」,REQ-20261022-008)。"""
    wd = Path(watch_dir)
    st = _load()
    vids = 0
    try:
        for entry in wd.iterdir():
            if entry.name.startswith("."):
                continue
            if entry.is_dir():
                vids += len(_videos_in(entry))
            elif entry.suffix.lower() in VIDEO_EXTS:
                vids += 1
    except OSError:
        pass
    pending = sum(1 for j in st["jobs"].values() if j.get("state") == "queued")
    last = ""
    ts = st.get("last_scan_ts") or 0
    if ts:
        last = time.strftime("%m-%d %H:%M", time.localtime(float(ts)))
    return {"last_scan": last or "尚未检查", "videos": vids,
            "pending": pending,
            "interval_min": int(_cfg().get("scan_interval_min", 120)),
            "watch_dir": str(wd)}


def target_stable(path):
    """目标是否已静置稳定(供 UI 说明「等待录像写完」)。"""
    try:
        return _stable(Path(path), int(_cfg().get("stable_min", 10)) * 60)
    except OSError:
        return False


def mark_scan(next_ts):
    """记录下一次扫描时刻(UI 显示「下次检查 HH:MM」)。"""
    state = _load()
    state["next_scan_ts"] = float(next_ts or 0)
    _save(state)


def next_scan_ts(state=None):
    st = state if state is not None else _load()
    try:
        return float(st.get("next_scan_ts") or 0)
    except (TypeError, ValueError):
        return 0.0


def progress_snapshot():
    """当前跑链进度(副本):{session: {stage,index,total,parts,ts,...}}。"""
    with _PROGRESS_LOCK:
        return {k: dict(v) for k, v in _PROGRESS.items()}


def running_progress():
    """正在跑的单个任务进度(含 frac/eta 视图);无则 None。"""
    snap = progress_snapshot()
    for s, v in snap.items():
        if "finished_ts" not in v:
            out = dict(v)
            done = v.get("done")
            if isinstance(done, set):
                out["done"] = set(done)     # 副本防外部改共享 set
            out.update(_progress_view(v))
            out["session"] = s              # 任务行匹配用(_wait_reason)
            return out
    return None


def _drain_proc(proc, on_line=None, keep=2000):
    """读子进程 stdout 到 EOF 并**等待退出**,返回 (returncode, 尾部输出)。

    2026-10-27 修复(r7 引入的回归):此前逐行读完流后直接取
    proc.returncode——Python 只在 wait() 后才回填它,EOF 不等于已收割,
    导致**正常完成的跑链也被记 rc=None,任务误判失败并重试**(mk4 场次
    「退出码None」即此)。EOF 后 wait 兜底 60s,仍挂着才强杀(防僵尸)。
    on_line: 每行回调(进度登记);行数超 4000 滚动裁剪防吃内存。"""
    lines = []
    for line in proc.stdout or []:
        lines.append(line)
        if len(lines) > 4000:
            del lines[:1000]
        if on_line:
            on_line(line.rstrip("\n"))
    try:
        proc.wait(timeout=60)               # EOF=子进程已关流,退出在即
    except subprocess.TimeoutExpired:       # 极端:EOF 后仍挂着 -> 强杀
        proc.kill()
        proc.wait(timeout=10)
    return proc.returncode, "".join(lines)[-keep:]


def run_pipeline_subprocess(target_path):
    """跑链子进程（与 pet 同配方：exe 形态自身即 CLI；UTF-8；管道收集）。

    显式带 --run-eval：后台日任务链曾漏传该参数，评估包上传后打标站
    0 片段、均须服务端人工补跑（REQ-20261021-002 根因位）；CLI 虽已
    默认开启，此处仍显式传防默认再漂移。"""
    global _CURRENT
    if getattr(sys, "frozen", False):
        args = [sys.executable, "pipeline", target_path, "--run-eval"]
    else:
        args = [sys.executable, "-X", "utf8", "-m", "toolbox",
                "pipeline", target_path, "--run-eval"]
    env = dict(os.environ, PYTHONUTF8="1", PYTHONIOENCODING="utf-8")
    # 注意:局部变量 proc 遮蔽模块名——NO_WINDOW 取模块别名(2026-10-22
    # pet_e2e 实测 UnboundLocalError 教训,勿把 creationflags 写在赋值前)
    proc = subprocess.Popen(args, cwd=str(ROOT), env=env,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            text=True, encoding="utf-8", errors="replace",
                            creationflags=_proc_mod().NO_WINDOW)
    _CURRENT = proc
    # 排空+等待由 _drain_proc 负责(含 returncode 修复:EOF 后必须 wait);
    # 逐行回调把阶段推进登记到 _PROGRESS,供 UI 显示「正在做什么」
    # (REQ-20261022-008)。
    session = (Path(target_path).name if Path(target_path).is_dir()
               else Path(target_path).parent.name)
    _progress_begin(session)
    try:
        rc, tail = _drain_proc(
            proc, on_line=lambda ln: _progress_line(session, ln))
    finally:
        _CURRENT = None
        _progress_end(session)
    return rc, tail


def process_pending(watch_dir, runner=run_pipeline_subprocess, limit=1):
    """一个调度节拍：回收租约 → 扫描入队 → 顺序跑 limit 个任务。
    返回 (new_enqueued, [(session, ok, note), ...])。"""
    expire_leases()
    new = scan(watch_dir)
    results = []
    for _ in range(limit):
        j = claim_next()
        if not j:
            break
        try:
            rc, log = runner(j["path"])
        except Exception as e:                 # noqa: BLE001 子进程异常按失败记
            rc, log = 1, str(e)
        ok = rc == 0
        finish(j["key"], ok, "跑链成功" if ok else f"退出码{rc}：{log[-400:]}")
        results.append((j["session"], ok, log if not ok else ""))
    return new, results


def remove(key):
    """用户删减任务(REQ-20261022-002):从队列移除该记录(不动磁盘文件)。
    running 任务树杀(正在跑的识别链立即终止,与 pet 关窗同法)。"""
    state = _load()
    j = state["jobs"].get(key)
    if not j:
        return None
    if j["state"] == "running":
        kill_current()
    state["jobs"].pop(key)
    _save(state)
    return j


def _purge_legacy_tutorial(state=None):
    """旧版新手教程遗留清理(REQ-20261022-005):队列记录 + 监控目录下的
    教程沙盒 + data/tutorial 素材缓存。幂等;无遗留时不写盘不删目录。
    返回清理的任务条数。"""
    import shutil
    st = state if state is not None else _load()
    jobs = st.get("jobs") or {}
    legacy = [k for k, j in jobs.items()
              if j.get("session") == LEGACY_TUTORIAL_SESSION]
    for k in legacy:
        j = jobs.pop(k)
        pth = j.get("path") or ""
        try:
            if pth and Path(pth).is_dir():
                shutil.rmtree(pth, ignore_errors=True)
        except OSError:
            pass
    if legacy:
        _save(st)
        print(f"旧版新手教程遗留已清理:{len(legacy)} 个队列任务")
    try:
        if LEGACY_TUTORIAL_DIR.is_dir():
            shutil.rmtree(LEGACY_TUTORIAL_DIR, ignore_errors=True)
    except OSError:
        pass
    return len(legacy)


def summary():
    """状态视图：{queued,running,done,failed:[...最近失败…]}。"""
    st = _load()["jobs"]
    out = {"queued": 0, "running": 0, "done": 0, "failed": []}
    for j in st.values():
        if j["state"] in ("queued", "running", "done"):
            out[j["state"]] += 1
        elif j["state"] == "failed":
            out["failed"].append(j["session"])
    return out


def jobs_view():
    """任务列表视图(REQ-20261022-002 优化):[{key,session,state,note}] 按
    入队序——供 UI 呈现每任务状态+删减。"""
    st = _load()["jobs"]
    out = []
    for k, j in sorted(st.items(), key=lambda kv: kv[1].get("enqueued_at", "")):
        out.append({"key": k, "session": j["session"], "state": j["state"],
                    "note": j.get("note", ""),
                    "taken_ts": j.get("taken_ts", 0),
                    "enqueued_at": j.get("enqueued_at", ""),
                    "path": j.get("path", "")})
    return out


# ---------------- 后台日任务标记（REQ-20261019-013：一天恰好一轮） ----------------

def last_daily_run():
    """最近一次后台日任务的日期（YYYY-MM-DD）；未跑过返回 ""。"""
    return str(_load().get("last_daily", ""))


def mark_daily_run():
    """标记今日已跑（原子写，与队列同文件持久化）。"""
    import datetime
    state = _load()
    state["last_daily"] = datetime.date.today().isoformat()
    _save(state)


# ------- 扫描/生产分离：24h 滚动窗口模型（REQ-20260921-001 规则 2-4） -------
#
# 规则重述（应用端职责部分）：
# - 每 2h 扫描（稳定入队，既有 scan；扫描≠生产）；生产窗口=北京时间每日
#   04:00（daily_hour）；开机时 24h 未生产则立即补跑（条件不豁免）；
# - 触发阈值：近 24h 新增录像总字节**严格>** threshold_gb（十进制 GB，
#   默认 2）才启动；「新增」= 稳定文件 mtime 终值落在滚动 24h 窗口内
#   （正在写入的不算，历史素材 mtime 旧自然排除、不重复计数）；
# - 滚动 24h 最多一次生产（客户端口径）：窗口内已触发过即不再触发；
#   服务端 production_groups 窗口去重为最终权威（409 window_conflict）。
# 规则 5「无新录像不生产、当日权益照算」：阈值不足=生产时刻照常消耗。

PRODUCTION_WINDOW_S = 86400          # 滚动生产窗口（与服务端建组窗口同口径）


def _target_covered(target, state):
    """目标是否「已处理过」(REQ-20261026-001 规则1):同路径存在 done 任务,
    且处理时内容指纹(done_key,旧任务回退 key)与当前一致。内容变了、
    在队列/失败、从未入队 → 都算未处理。"""
    try:
        cur = job_key(target)
    except OSError:
        return True                       # 取不到键(目标消失):不计入
    tp = str(target)
    for j in state["jobs"].values():
        if j.get("path") != tp:
            continue
        if j.get("state") != "done":
            return False                  # 同路径任务还在等/失败 → 未处理
        dk = j.get("done_key") or j.get("key")
        return dk == cur                  # done 且指纹一致 → 已处理
    return False                          # 从未入队 → 未处理


def unprocessed_bytes(watch_dir, cfg=None, state=None):
    """未处理录像总字节(REQ-20261026-001 规则1,替代 mtime 24h 窗口口径)。

    目标归属与 scan 同口径:根直下散装视频归为一个根目标(与目录任务聚合
    一致);子目录视频归其所在目录。只计「稳定」目标(写入中的不算,防止
    触发生产后文件仍在涨)。老素材只要没处理过就计入——修复「队列躺着
    永远不触发」(旧口径只看 mtime 近 24h,老录像新增恒为 0)。"""
    cfg = cfg if cfg is not None else _cfg()
    state = state if state is not None else _load()
    stable_sec = int(cfg.get("stable_min", 10)) * 60
    wd = Path(watch_dir)
    if not wd.is_dir():
        return 0.0
    groups = {}                           # 目标 -> [视频]
    for v in _videos_in(wd):
        tgt = v.parent if v.parent != wd else wd
        groups.setdefault(tgt, []).append(v)
    now = time.time()
    total = 0.0
    for tgt, vids in groups.items():
        try:
            # 判稳只看本组成员(不用 _stable 整目录递归——根散装组会被
            # 子目录里写入中的文件连坐);非空同 scan 口径
            stable = all(v.stat().st_size > 0
                         and now - v.stat().st_mtime >= stable_sec
                         for v in vids)
            if not stable:
                continue
            if _target_covered(tgt, state):
                continue
            total += sum(v.stat().st_size for v in vids)
        except OSError:
            continue
    return total


def threshold_bytes(cfg=None):
    """触发阈值字节数（十进制 GB；REQ 默认值口径）。"""
    cfg = cfg if cfg is not None else _cfg()
    return float(cfg.get("threshold_gb", 2)) * 1e9


def new_recordings_bytes(watch_dir, window_s=PRODUCTION_WINDOW_S,
                         now=None, cfg=None):
    """近 window_s 新增录像总字节：仅稳定目标内、mtime 终值在窗口内的
    视频文件（完成写入时刻口径；正在写入/历史素材不计）。"""
    wd = Path(watch_dir)
    if not wd.is_dir():
        return 0
    cfg = cfg if cfg is not None else _cfg()
    stable_sec = int(cfg.get("stable_min", 10)) * 60
    now = now if now is not None else time.time()
    cutoff = now - window_s
    total = 0
    for entry in sorted(wd.iterdir()):
        if entry.is_dir():
            if not _stable(entry, stable_sec, now=now):
                continue                       # 组内有文件仍在写 → 整组未完成
            vids = _videos_in(entry)
        elif entry.suffix.lower() in VIDEO_EXTS:
            if not _stable(entry, stable_sec, now=now):
                continue
            vids = [entry]
        else:
            continue
        for v in vids:
            try:
                st = v.stat()
            except OSError:
                continue
            if cutoff < st.st_mtime <= now:
                total += st.st_size
    return total


# 工作时段(用户 2026-10-28 指示:去掉滚动 24h 窗口,改为每日固定时段生产)
WORK_START_H, WORK_END_H = 4, 12          # 凌晨 4 点 ~ 中午 12 点(含头不含尾)


def _in_work_hours(now=None, cfg=None):
    """当前是否在生产工作时段内([start, end) 小时,支持跨零点配置)。"""
    cfg = cfg if cfg is not None else _cfg()
    now = now if now is not None else time.time()
    import datetime
    h = datetime.datetime.fromtimestamp(now).hour
    start = int(cfg.get("work_start_hour", WORK_START_H))
    end = int(cfg.get("work_end_hour", WORK_END_H))
    if start <= end:
        return start <= h < end
    return h >= start or h < end          # 跨零点(如 22~6)


class _FileTime(ctypes.Structure):
    _fields_ = [("lo", ctypes.c_ulong), ("hi", ctypes.c_ulong)]


class _SysTimes(ctypes.Structure):
    """GetSystemTimes 出参(idle/kernel/user 各为 FILETIME,100ns 单位;
    kernel 含 idle——占比用 (total-idle)/total)。"""
    _fields_ = [("idle", _FileTime), ("kernel", _FileTime),
                ("user", _FileTime)]


def _get_system_times():
    st = _SysTimes()
    if not ctypes.windll.kernel32.GetSystemTimes(
            ctypes.byref(st.idle), ctypes.byref(st.kernel),
            ctypes.byref(st.user)):
        raise OSError("GetSystemTimes failed")

    def ft(f):
        return (f.hi << 32) | f.lo
    return ft(st.idle), ft(st.kernel), ft(st.user)


def cpu_percent(sample=0.5):
    """整机 CPU 占用(0~100):GetSystemTimes 前后差分(win32 自带,免子进程
    免 psutil;wmic 在新系统已移除不可用)。采样 sample 秒;失败返回 None
    (调用方按不退让处理)。busy=kernel+user-idle(kernel 含 idle 是
    FILETIME 语义,直接 (total-idle)/total 即可)。"""
    if sys.platform != "win32":
        return None
    try:
        i1, k1, u1 = _get_system_times()
        time.sleep(max(0.05, sample))
        i2, k2, u2 = _get_system_times()
        total = (k2 + u2) - (k1 + u1)
        idle = i2 - i1
        if total <= 0:
            return None
        return round((total - idle) * 100.0 / total, 1)
    except (OSError, AttributeError):
        return None


def production_due(daily_hour=4, boot=False, cfg=None, state=None,
                   now=None):
    """生产是否到期(2026-10-28 新规则,用户指示):

    - **去掉滚动 24h 窗口**——此前"跳过也消耗窗口"导致队列最长白等一天;
    - 生产只看**工作时段**:默认凌晨 4:00 ~ 中午 12:00(work_start_hour/
      work_end_hour 可配,支持跨零点);时段内随时可触发、跑完即可再触发
      (频率由任务有无与 CPU 退让自然约束);
    - boot(开机补跑)= 工作时段内开机即触发;时段外开机不动,等下个时段;
    - CPU 退让在 pet 调用侧(这里只判时间)。
    返回 (due: bool, why: str)。"""
    cfg = cfg if cfg is not None else _cfg()
    state = state if state is not None else _load()
    now = now if now is not None else time.time()
    if _in_work_hours(now, cfg):
        if boot:
            return True, "工作时段开机补跑"
        return True, "工作时段内"
    import datetime
    h = datetime.datetime.fromtimestamp(now).hour
    start = int(cfg.get("work_start_hour", WORK_START_H))
    end = int(cfg.get("work_end_hour", WORK_END_H))
    return False, f"非工作时段（{start:02d}:00-{end:02d}:00 外，现在 {h:02d} 点）"


def mark_production_run(state=None, now=None):
    """记录一次生产触发（无论阈值是否满足——生产时刻照常消耗；绑定门
    未过等「暂缓」不记，由 pet 侧负责区分）。"""
    state = state if state is not None else _load()
    state["last_production_ts"] = now if now is not None else time.time()
    _save(state)


def last_production_ts(state=None):
    state = state if state is not None else _load()
    return float(state.get("last_production_ts") or 0)
