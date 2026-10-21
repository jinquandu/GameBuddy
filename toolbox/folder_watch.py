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
from toolbox.config import DATA, ROOT, load_config

QUEUE_FILE = DATA / ".folder_watch.json"
VIDEO_EXTS = {".mp4", ".flv", ".ts", ".mkv", ".mov"}


def _cfg():
    return (load_config().get("folder_watch") or {})


def _now():
    return time.strftime("%Y-%m-%d %H:%M:%S")


def _load():
    try:
        return json.loads(QUEUE_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"jobs": {}}


def _save(state):
    QUEUE_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = QUEUE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=1),
                   encoding="utf-8")
    tmp.replace(QUEUE_FILE)


def job_key(p: Path):
    """任务键。文件=自身 stat；目录=对内部视频清单逐项哈希——目录自身的
    stat 在「改写内部文件」时不变化（NTFS 只在建删改名时动目录 mtime），
    按目录 stat 做键会漏检内容变化（2026-10-19 回归实测）。"""
    h = hashlib.sha1(str(p).encode("utf-8"))
    if p.is_dir():
        for v in _videos_in(p):
            st = v.stat()
            h.update(f"|{v.name}|{st.st_size}|{int(st.st_mtime)}".encode())
    else:
        st = p.stat()
        h.update(f"|{st.st_size}|{int(st.st_mtime)}".encode())
    return h.hexdigest()


def _videos_in(target: Path):
    if target.is_file():
        return [target] if target.suffix.lower() in VIDEO_EXTS else []
    return sorted(p for p in target.iterdir()
                  if p.is_file() and p.suffix.lower() in VIDEO_EXTS)


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
    """稳定目标入队（幂等）。返回本次新入队数。"""
    wd = Path(watch_dir)
    if not wd.is_dir():
        return 0
    state = state if state is not None else _load()
    cfg = _cfg()
    stable_sec = int(cfg.get("stable_min", 10)) * 60
    new = 0
    for entry in sorted(wd.iterdir()):
        target = None
        if entry.is_dir():
            vids = _videos_in(entry)
            if vids and _stable(entry, stable_sec):
                target = entry
        elif entry.suffix.lower() in VIDEO_EXTS and _stable(entry, stable_sec):
            target = entry
        if target is None:
            continue
        try:
            key = job_key(target)
        except OSError:
            continue
        if key in state["jobs"]:
            continue                       # 去重：任何状态都不重复入队
        state["jobs"][key] = {
            "key": key, "path": str(target),
            "session": target.stem if target.is_file() else target.name,
            "state": "queued", "attempts": 0,
            "enqueued_at": _now(), "updated_at": _now(), "note": ""}
        new += 1
    if new:
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
    proc = subprocess.Popen(args, cwd=str(ROOT), env=env,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            text=True, encoding="utf-8", errors="replace")
    _CURRENT = proc
    try:
        out, _ = proc.communicate()
    finally:
        _CURRENT = None
    return proc.returncode, (out or "")[-2000:]


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
