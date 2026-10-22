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
import time
from pathlib import Path

from toolbox import proc


def _proc_mod():
    """模块别名(局部 proc 变量遮蔽模块名时的取用通道)。"""
    return proc
from toolbox.config import DATA, ROOT, load_config

QUEUE_FILE = DATA / ".folder_watch.json"
VIDEO_EXTS = {".mp4", ".flv", ".ts", ".mkv", ".mov"}
TUTORIAL_SESSION = "新手体验-demo"    # 新手教程沙盒会话名(REQ-20261022-002):
                                      # 队列消费时拦截,不跑真识别链
SCAN_DEPTH = 3        # 监控根下递归深度上限（REQ-20261021-004）：目标目录
                      # 最深=监控根/x/y/（二级场次，如 根/<游戏>/<场次>/），
                      # 再深不扫——防误扫用户整盘；只看根直下=旧行为


def _cfg():
    return (load_config().get("folder_watch") or {})


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
    running>queued>done/failed。幂等;不影响 path 唯一的目录会话(如教程)。"""
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
        # 教程会话(新手体验-demo)永远独立保留,不参与归并
        if keep_j.get("session") == TUTORIAL_SESSION:
            continue
        # 散装组:保留键改为目录键(目录任务口径),session 取目录名
        if not Path(keep_j.get("path", "")).is_dir() and len(items) > 1:
            keep_j["path"] = grp
            keep_j["session"] = Path(grp).name
        for k, _ in items[1:]:
            if jobs[k].get("session") == TUTORIAL_SESSION:
                continue                   # 教程会话不吞
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
    # new>0 或发生目录任务聚合(note/键更新)都要落盘
    if new or any(j.get("note") == "内容已更新"
                  for j in state["jobs"].values()):
        _save(state)
    return new


def expire_leases(state=None):
    """崩溃租约回收：running 超时 → queued（attempts+1，超限记 failed）。"""
    state = state if state is not None else _load()
    cfg = _cfg()
    lease_sec = int(cfg.get("lease_min", 120)) * 60
    amax = int(cfg.get("attempts_max", 3))
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


def claim_next(state=None, only_session=None):
    """领取一个 queued 任务 → running（记租约）。无任务返回 None。
    only_session(REQ-20261022-002):只领指定会话(教程流程用,防止把用户
    真实场次一起拉起来跑),不匹配的任务原样留在队列。"""
    state = state if state is not None else _load()
    for j in sorted(state["jobs"].values(), key=lambda x: x["enqueued_at"]):
        if j["state"] == "queued":
            if only_session and j["session"] != only_session:
                continue
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
    amax = int(_cfg().get("attempts_max", 3))
    j.pop("taken_ts", None)
    if ok:
        j["state"], j["note"] = "done", note
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
    try:
        out, _ = proc.communicate()
    finally:
        _CURRENT = None
    return proc.returncode, (out or "")[-2000:]


def process_pending(watch_dir, runner=run_pipeline_subprocess, limit=1,
                    only_session=None):
    """一个调度节拍：回收租约 → 扫描入队 → 顺序跑 limit 个任务。
    返回 (new_enqueued, [(session, ok, note), ...])。
    only_session(REQ-20261022-002):只消费指定会话(教程流程),其余任务
    留在队列不受影响。"""
    expire_leases()
    new = scan(watch_dir)
    results = []
    for _ in range(limit):
        j = claim_next(only_session=only_session)
        if not j:
            break
        # 教程会话拦截(REQ-20261022-002):不跑真识别链/真上传,直接标
        # done——真实机制的可视化部分(扫描/队列/徽标)已经走完,处理本身
        # 由服务端 C2S-16 短路(客户端演出节奏在 pet 侧)。
        if j.get("session") == TUTORIAL_SESSION:
            finish(j["key"], True, "新手教程会话(服务端短路,不跑识别链)")
            results.append((j["session"], True, "tutorial"))
            continue
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
    """任务列表视图(REQ-20261022-002 优化):[{key,session,state,at}] 按
    入队序——供 UI 呈现每任务状态+删减。"""
    st = _load()["jobs"]
    out = [{"key": k, "session": j["session"], "state": j["state"],
            "note": j.get("note", "")}
           for k, j in sorted(st.items(),
                              key=lambda kv: kv[1].get("enqueued_at", ""))]
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


def production_due(daily_hour=4, boot=False, cfg=None, state=None,
                   now=None):
    """生产是否到期：开机补跑（boot，无小时门）或 ≥daily_hour，且滚动
    24h 窗口内未生产过。返回 (due: bool, why: str)——why 供日志留痕。"""
    cfg = cfg if cfg is not None else _cfg()
    state = state if state is not None else _load()
    now = now if now is not None else time.time()
    daily_hour = int(cfg.get("daily_hour", daily_hour))
    window_s = int(cfg.get("production_window_s", PRODUCTION_WINDOW_S))
    last = float(state.get("last_production_ts") or 0)
    if last and now - last < window_s:
        return False, f"滚动窗口内已生产（{int(now - last) // 3600}h 前）"
    if boot:
        return True, "开机补跑（24h 未生产）"
    import datetime
    if datetime.datetime.fromtimestamp(now).hour >= daily_hour:
        return True, f"到达生产时刻（≥{daily_hour:02d}:00）"
    return False, f"未到生产时刻（<{daily_hour:02d}:00）"


def mark_production_run(state=None, now=None):
    """记录一次生产触发（无论阈值是否满足——生产时刻照常消耗；绑定门
    未过等「暂缓」不记，由 pet 侧负责区分）。"""
    state = state if state is not None else _load()
    state["last_production_ts"] = now if now is not None else time.time()
    _save(state)


def last_production_ts(state=None):
    state = state if state is not None else _load()
    return float(state.get("last_production_ts") or 0)
