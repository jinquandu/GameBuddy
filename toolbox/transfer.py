"""应用端 <-> 服务端搬运（两步上传契约）。

传输策略（2026-09-17 决策，钢牙/小晨/香菜三场实测支撑）：
- 评估包 push（每场 ~1-1.5G，约源片 25%）：events/transcripts JSON + 击倒/拾取/
  语音切片 + L1 缓存。打标站即刻可开工，落选场次永远不付全量。
- 成片包 push（~4-5G，仅被选中做片的场次）：源片全量，服务端 highlight/remix
  保持回源片自由重切（改留白、镜头边界吸附、labeling pick 重选段）。
- pull：服务端 remix 产物（成片 + cover + publish.json）拉回应用端手动上传。

服务端布局（remote_root = /opt/dashijie-eval-data）：
    data/      服务端 toolbox 工作树（TOOLBOX_DATA_DIR 挂载点，目录结构与
               应用端 data/ 一致：asr/ reports/ knockdowns/ pickups/ voice/ remixes/）
    sessions/<场次>/
        manifest.json   最近一次 push 的清单（客户端写）
        session.json    服务端状态（import-session 写/更新，持久）
        video/P*.mp4    源片（成片包阶段才上传）

排除约定：l2/（服务端人工标注产物，上送会覆盖）与服务端可重建物
（scores.json / review.html / _frames/ / _scancache.json）不上送。
"""
import json
import os
import re
import shlex
import shutil
import subprocess
import tempfile
from datetime import datetime
from pathlib import Path

from toolbox.config import DATA, REMIXES, REPORTS, load_config

# 端侧<->服务端契约版本：与 docs/SERVER-INTERFACE.md 同步维护（先改文档再改
# 此值）；写入 manifest.json 供服务端检测端侧契约版本。分级见契约文档 §0。
CONTRACT_VERSION = "1.0.0"


# ---------------------------------------------------------------- 会话发现

def _slug(s, limit=40):
    from toolbox.knockdown import _slug as _k
    return _k(s, limit)


def _canonical_rpt(video: Path):
    """事件流在服务端的规范目录名：<分片stem>_full（与 knockdown 链路自检写出的
    命名一致；事件池 discover_pool 只认 *_full，本地历史裸名目录在搬运时归一）。"""
    return _slug(video.stem) + "_full"


def _find_events_rpt(video: Path):
    """按首行 src 匹配该分片的 events.jsonl 报告目录。优先级：规范 _full 名 >
    其他 _full 目录 > 任意目录（历史裸名，可能只含小扫描子集）。"""
    if not REPORTS.exists():
        return None
    cands = []
    for rpt in sorted(REPORTS.glob("*/events.jsonl")):
        lines = [l for l in rpt.read_text(encoding="utf-8").splitlines() if l.strip()]
        if not lines:
            continue
        try:
            first = json.loads(lines[0])
        except json.JSONDecodeError:
            continue
        if first.get("src") == str(video):
            cands.append(rpt.parent)
    if not cands:
        return None
    canonical = REPORTS / _canonical_rpt(video)
    if canonical in cands:
        return canonical
    full = [c for c in cands if c.name.endswith("_full")]
    return (full or cands)[0]


def discover_session(session):
    """按各链路既定命名规则聚拢一个场次的所有产物（缺失项为 None/[]）。"""
    from toolbox.asr import find_transcript_dir
    from toolbox.knockdown import collect_videos
    target = Path(session).expanduser()
    videos = collect_videos(session)
    session_name = (videos[0].parent.name if len(videos) > 1 or target.is_dir()
                    else videos[0].stem)

    parts, voice_dirs, missing, local_rpts = [], [], [], []
    for v in videos:
        asr_dir = find_transcript_dir(v)
        rpt = _find_events_rpt(v)
        if not asr_dir:
            missing.append(f"{v.name[:30]}：无 ASR 转写（先跑 toolbox asr）")
        if not rpt:
            missing.append(f"{v.name[:30]}：无事件流（先跑 toolbox detect）")
        entry = {"client_video": str(v), "filename": v.name,
                 "asr_dir": asr_dir.name if asr_dir else None,
                 # 服务端统一落规范 _full 名：事件池/报告只认 *_full
                 "events_rpt": _canonical_rpt(v) if rpt else None}
        voice_dir = DATA / "voice" / _slug(asr_dir.name, 40) if asr_dir else None
        if voice_dir and voice_dir.is_dir():
            voice_dirs.append(voice_dir)
            entry["voice_dir"] = voice_dir.name
        parts.append(entry)
        if rpt:
            local_rpts.append(rpt)

    kd_dir = DATA / "knockdowns" / _slug(session_name, 60)
    pk_dir = DATA / "pickups" / _slug(session_name, 60)
    return {
        "session": session_name,
        "parts": parts,
        "asr_dirs": [DATA / "asr" / p["asr_dir"] for p in parts if p["asr_dir"]],
        "events_rpts_local": local_rpts,
        "knockdowns_dir": kd_dir if kd_dir.is_dir() else None,
        "pickups_dir": pk_dir if pk_dir.is_dir() else None,
        "voice_dirs": sorted(set(voice_dirs)),
        "missing": missing,
    }


def _du(paths):
    total = 0
    for p in map(Path, paths):
        if p.is_file():
            total += p.stat().st_size
        elif p.is_dir():
            total += sum(f.stat().st_size for f in p.rglob("*") if f.is_file())
    return total


# ---------------------------------------------------------------- 进程执行

def _ssh(alias, remote_cmd, check=True):
    """控制通道（mkdir/import 触发）：Windows 固定用原生 OpenSSH（读
    ~/.ssh/config 无障碍）；MSYS ssh 仅供 rsync 进程内使用（见 _rsync 注释），
    PATH 里排在前面时不能让它劫持这里的 "ssh"。"""
    bin_ = "ssh"
    if os.name == "nt":
        native = Path(r"C:\Windows\System32\OpenSSH\ssh.exe")
        if native.exists():
            bin_ = str(native)
    return subprocess.run([bin_, alias, remote_cmd],
                          check=check, text=True, capture_output=True)


_RSYNC_CACHE = {}


def _rsync_tool():
    """GNU rsync 探测：路径含中文/括号必须走参数保护（-s）。历史名字
    --protect-args 在 3.5 改名 --secluded-args，故用功能探测而非 help 文案。
    openrsync（macOS /usr/bin/rsync）两个名字都不认，自动跳过。"""
    if "bin" in _RSYNC_CACHE:
        return _RSYNC_CACHE["bin"]
    cands = ["/opt/homebrew/bin/rsync", "/usr/local/bin/rsync",
             shutil.which("rsync") or ""]
    for c in cands:
        if not c or not Path(c).exists():
            continue
        for flag in ("--secluded-args", "--protect-args"):
            try:
                r = subprocess.run([c, flag, "--version"],
                                   capture_output=True, timeout=10)
            except Exception:
                break
            if r.returncode == 0:
                _RSYNC_CACHE["bin"] = (c, flag)
                return c, flag
    raise RuntimeError("需要 GNU rsync（远端路径含中文/括号，必须支持参数保护）。"
                       "安装：macOS `brew install rsync`；Windows "
                       "`scoop install rsync` 或 MSYS2；Linux `dnf install rsync`")


def _msys_local(path: str) -> str:
    """Windows 下 MSYS rsync 的本地路径规范化（2026-09-18 实测）：
    rsync 把 `C:\\x` 解析成远端（主机 C，"源和目标都是远端"报错），须转
    MSYS2 的 cygdrive 形式 `/cygdrive/c/x`；远端规格（ecs:/opt/...）不含
    单字母盘符前缀，不受影响。"""
    if os.name != "nt":
        return path
    m = re.match(r"^([A-Za-z]):[\\/](.*)$", path)
    if m:
        return f"/cygdrive/{m.group(1).lower()}/{m.group(2).replace(chr(92), '/')}"
    if path.startswith("\\\\"):                          # UNC -> 正斜杠
        return path.replace(chr(92), "/")
    return path


def _rsync(src: str, dst: str, excludes=(), dry=False):
    rsbin, prot = _rsync_tool()
    cmd = [rsbin, "-a", "--partial", prot, "--modify-window=1"]
    if not dry:
        cmd.append("--progress")
    for e in excludes:
        cmd += ["--exclude", e]
    if dry:
        cmd.append("--dry-run")
    cmd += [_msys_local(src), _msys_local(dst)]
    env = None
    if os.name == "nt":
        # Windows + MSYS rsync 配方（2026-09-18 实证，缺一不可）：
        # 1. msys rsync 必须配 msys ssh（同包生态）——原生 Windows OpenSSH
        #    与其协议层不兼容（connection unexpectedly closed, 0 bytes）；
        #    PATH 前置 rsync 所在目录让 execvp("ssh") 找到配套 ssh.exe；
        # 2. MSYS2_ARG_CONV_EXCL=* ：msys exec 原生程序时会把看似 POSIX 路径
        #    的 rsync 协议参数转成 Windows 路径，必须禁用；
        # 3. RSYNC_RSH 显式给 ssh 指定 -F/-o UserKnownHostsFile（msys ssh 无
        #    /etc/passwd，HOME 解析落到不存在的 /home/<user>，读不到 config）。
        home = str(Path.home()).replace("\\", "/")
        env = dict(os.environ,
                   MSYS2_ARG_CONV_EXCL="*",
                   RSYNC_RSH=(f"ssh -F {home}/.ssh/config "
                              f"-o UserKnownHostsFile={home}/.ssh/known_hosts"))
        env["PATH"] = str(Path(rsbin).parent) + os.pathsep + env.get("PATH", "")
    r = subprocess.run(cmd, env=env)
    if r.returncode != 0:
        raise RuntimeError(f"rsync 失败（退出码 {r.returncode}）：{src} -> {dst}")


def _rsync_to(alias, remote_path, src, excludes=(), dry=False, raw_src=False):
    """本端 -> 远端（GNU rsync -s 原样传路径，无 shell 转义）。"""
    s = src if raw_src else f"{src}/"
    _rsync(s, f"{alias}:{remote_path}", excludes, dry)


def _rsync_from(alias, remote_path, dst, excludes=(), dry=False):
    """远端 -> 本端。"""
    _rsync(f"{alias}:{remote_path}/", f"{dst}/", excludes, dry)


def _mkdir(alias, remote_dir, dry=False):
    if dry:
        print(f"  [dry] mkdir -p {remote_dir}")
        return
    _ssh(alias, f"mkdir -p {shlex.quote(remote_dir)}")


# ---------------------------------------------------------------- push

EVAL_EXCLUDES = {
    "asr": ("audio_16k.wav", "*.log"),
    "knockdowns": ("l2/", "_frames/", "review.html", "scores.json"),
    "pickups": ("l2/", "_frames/", "review.html", "scores.json", "_scancache.json"),
    "voice": ("review.html",),
}


def _tenant_root(cfg, tenant=None):
    """多租户方案A：数据按 /opt/dashijie-eval-data/tenants/<租户>/ 分前缀。"""
    t = tenant if tenant is not None else cfg.get("tenant", "default")
    if not t or not re.match(r"^[A-Za-z0-9_-]{1,32}$", t):
        raise ValueError(f"租户名不合法: {t!r}（仅限字母数字-_，≤32 字）")
    return f"{cfg['remote_root']}/tenants/{t}"


def push(session, mode="eval", dry=False, run_eval=False, no_import=False,
         tenant=None):
    """上传会话产物到服务端。mode: eval=评估包（默认）| video=成片包（源片）。

    评估包 rsync 直达服务端 data/ 工作树；两种模式完成后（除非 --no-import）
    均经 ssh 触发容器内 import-session 路径重映射+状态登记，--run-eval 评估包
    可连跑评估链。多租户：--tenant 覆盖 server.tenant 配置。
    """
    cfg = load_config()["server"]
    alias, remote = cfg["ssh_alias"], _tenant_root(cfg, tenant)
    disc = discover_session(session)

    if mode == "video":
        vids = [Path(p["client_video"]) for p in disc["parts"]]
        if not vids or not all(v.is_file() for v in vids):
            raise RuntimeError(f"源片不存在：{session}")
        total = _du(vids)
        vdir = f"{remote}/sessions/{disc['session']}/video"
        print(f"成片包：{len(vids)} 个源片，约 {total/1e9:.2f}GB -> {vdir}")
        _mkdir(alias, vdir, dry)
        for v in vids:
            print(f"  {v.name}")
            _rsync_to(alias, f"{vdir}/{v.name}", str(v), dry=dry, raw_src=True)
        _write_manifest(alias, remote, disc, mode="video", dry=dry)
    else:
        if disc["missing"]:
            # ASR/events 缺失不硬阻断：报告与击倒/拾取打标不依赖 ASR；
            # 完整度如实记录进 manifest，打标站显示。至少得有一组可上传产物。
            print("警告：以下产物缺失，将如实登记为 incomplete：")
            for m in disc["missing"]:
                print(f"  - {m}")
        mappings = [(d, f"{remote}/data/asr/{d.name}", EVAL_EXCLUDES["asr"])
                    for d in disc["asr_dirs"]]
        parts_with_rpt = [p for p in disc["parts"] if p.get("events_rpt")]
        mappings += [(r, f"{remote}/data/reports/{p['events_rpt']}", ())
                     for r, p in zip(disc["events_rpts_local"], parts_with_rpt)]
        if disc["knockdowns_dir"]:
            mappings.append((disc["knockdowns_dir"],
                             f"{remote}/data/knockdowns/{disc['knockdowns_dir'].name}",
                             EVAL_EXCLUDES["knockdowns"]))
        if disc["pickups_dir"]:
            mappings.append((disc["pickups_dir"],
                             f"{remote}/data/pickups/{disc['pickups_dir'].name}",
                             EVAL_EXCLUDES["pickups"]))
        mappings += [(v, f"{remote}/data/voice/{v.name}", EVAL_EXCLUDES["voice"])
                     for v in disc["voice_dirs"]]
        if not mappings:
            raise RuntimeError("没有可上传的产物")
        total = _du([m[0] for m in mappings])
        print(f"评估包：{len(mappings)} 组，约 {total/1e9:.2f}GB")
        for local, remote_dir, excludes in mappings:
            _mkdir(alias, remote_dir, dry)
            print(f"  -> {remote_dir.replace(remote + '/', '')}/")
            _rsync_to(alias, remote_dir, str(local), excludes, dry)
        _write_manifest(alias, remote, disc, mode="eval", dry=dry)

    if no_import:
        print("（跳过服务端 import）")
        return
    _trigger(alias, disc["session"], remote, do_eval=run_eval and mode == "eval",
             dry=dry)


def _write_manifest(alias, remote, disc, mode, dry=False):
    manifest = {
        "contract_version": CONTRACT_VERSION,
        "session": disc["session"],
        "mode": mode,
        "pushed_at": datetime.now().isoformat(timespec="seconds"),
        "parts": disc["parts"],
        "missing": disc["missing"],
        "lines": {
            "knockdowns": disc["knockdowns_dir"].name if disc["knockdowns_dir"] else None,
            "pickups": disc["pickups_dir"].name if disc["pickups_dir"] else None,
            "voice": [v.name for v in disc["voice_dirs"]],
        },
    }
    fd, name = tempfile.mkstemp(suffix=".json")
    os.close(fd)                    # Windows：句柄不关，文件锁定删不掉
    tmp = Path(name)
    tmp.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    sdir = f"{remote}/sessions/{disc['session']}"
    if dry:
        print(f"  [dry] manifest -> {sdir}/manifest.json")
    else:
        _mkdir(alias, sdir)
        _rsync_to(alias, f"{sdir}/manifest.json", str(tmp), raw_src=True)
    tmp.unlink(missing_ok=True)


def _trigger(alias, session, tenant_root, do_eval=False, dry=False):
    """ssh 触发服务端容器：import-session（+ 可选 eval）。
    多租户：数据根经 -e TOOLBOX_DATA_DIR 显式传给 exec 进程。"""
    container = load_config()["server"].get("container", "dashijie-eval")
    exec_prefix = (f"docker exec -e TOOLBOX_DATA_DIR={shlex.quote(tenant_root + '/data')} "
                   f"{shlex.quote(container)}")
    chain = [f"{exec_prefix} python3 -m toolbox import-session "
             f"{shlex.quote(session)} --remote-root {shlex.quote(tenant_root)}"]
    if do_eval:
        chain.append(f"{exec_prefix} python3 -m toolbox eval {shlex.quote(session)} "
                     f"--remote-root {shlex.quote(tenant_root)}")
    remote_cmd = " && ".join(chain)
    if dry:
        print(f"  [dry] ssh {alias} {remote_cmd}")
        return
    print(f"触发服务端: {remote_cmd}")
    r = _ssh(alias, remote_cmd, check=False)
    if r.stdout.strip():
        print(r.stdout.strip())
    if r.returncode != 0:
        raise RuntimeError(f"服务端 import/eval 失败:\n{r.stderr.strip()[:800]}")


# ---------------------------------------------------------------- pull

def pull(template=None, dry=False, tenant=None):
    """服务端 remix 产物拉回应用端（成片 + cover + publish.json，跳过 seg/ 等中间产物）。"""
    cfg = load_config()["server"]
    alias, remote = cfg["ssh_alias"], _tenant_root(cfg, tenant)
    if template:
        tpls = [template]
    else:
        r = _ssh(alias, f"ls -1 {shlex.quote(remote + '/data/remixes')}")
        tpls = [l.strip() for l in r.stdout.splitlines() if l.strip()]
    if not tpls:
        print("服务端暂无 remixes 产物")
        return
    for t in tpls:
        dst = REMIXES / t
        dst.mkdir(parents=True, exist_ok=True)
        print(f"  <- remixes/{t}/")
        _rsync_from(alias, f"{remote}/data/remixes/{t}", dst,
                    excludes=("seg/", "concat.txt", "edl.json"), dry=dry)
        if not dry:
            _localize_publish(dst, remote)
    print(f"已拉回 {len(tpls)} 个模板 -> {REMIXES}")


def _localize_publish(tdir: Path, remote_root):
    """publish.json 的 cover 等字段是服务端绝对路径；拉回后改写为本地同目录文件。"""
    pj = tdir / "publish.json"
    if not pj.is_file():
        return
    try:
        data = json.loads(pj.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return
    changed = False
    for k, v in list(data.items()):
        if isinstance(v, str) and v.startswith(remote_root + "/"):
            data[k] = str(tdir / Path(v).name)     # 封面等产物与成片同目录
            changed = True
    if changed:
        pj.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


# ---------------------------------------------------------------- 服务端 import-session

def import_session(session, remote_root=None):
    """【服务端】push 后收口：路径重映射 + 会话状态登记。幂等可重跑。

    仅改写"客户端绝对路径 -> 服务端 sessions/<场次>/video/ 绝对路径"的精确匹配
    （transcript.game、events.jsonl 每行 src）；已是服务端路径的行原样保留。
    """
    cfg = load_config()
    root = Path(remote_root or cfg["server"]["remote_root"])
    sdir = root / "sessions" / session
    if not (sdir / "manifest.json").is_file():
        raise FileNotFoundError(f"无 manifest：{sdir}/manifest.json（先在应用端 push）")
    manifest = json.loads((sdir / "manifest.json").read_text(encoding="utf-8"))

    remapped = unchanged = 0
    for part in manifest["parts"]:
        client_video = part.get("client_video")
        server_video = str(sdir / "video" / part["filename"])
        if not client_video:
            continue
        asr_dir = DATA / "asr" / part["asr_dir"] if part.get("asr_dir") else None
        if asr_dir:
            for jf in (asr_dir / "transcript.json", asr_dir / "speaker_labeled.json"):
                if not jf.is_file():
                    continue
                data = json.loads(jf.read_text(encoding="utf-8"))
                if data.get("game") == client_video:
                    data["game"] = server_video
                    jf.write_text(json.dumps(data, ensure_ascii=False, indent=2),
                                  encoding="utf-8")
                    remapped += 1
                else:
                    unchanged += 1
        rpt = DATA / "reports" / part["events_rpt"] if part.get("events_rpt") else None
        if rpt and (rpt / "events.jsonl").is_file():
            out = []
            for l in (rpt / "events.jsonl").read_text(encoding="utf-8").splitlines():
                if not l.strip():
                    continue
                e = json.loads(l)
                if e.get("src") == client_video:
                    e["src"] = server_video
                    remapped += 1
                else:
                    unchanged += 1
                out.append(json.dumps(e, ensure_ascii=False))
            (rpt / "events.jsonl").write_text("\n".join(out) + "\n", encoding="utf-8")

    state_p = sdir / "session.json"
    state = json.loads(state_p.read_text(encoding="utf-8")) if state_p.is_file() else {}
    state.update({
        "session": session,
        "imported_at": datetime.now().isoformat(timespec="seconds"),
        "mode": manifest.get("mode"),
        "video_ready": all((sdir / "video" / p["filename"]).is_file()
                           for p in manifest["parts"]),
        "parts": [dict(p, server_video=str(sdir / "video" / p["filename"]))
                  for p in manifest["parts"]],
        "lines": manifest.get("lines", {}),
    })
    state_p.write_text(json.dumps(state, ensure_ascii=False, indent=2),
                       encoding="utf-8")
    print(f"[{session}] 重映射 {remapped} 处 / 未匹配 {unchanged}；"
          f"video_ready={state['video_ready']} -> {state_p}")


# ---------------------------------------------------------------- 服务端 sessions 管理

def _tenant_sessions(troot: Path):
    """单租户 -> [{ts, dir, state, size}]（按 sessions/<场次>/session.json）。"""
    out = []
    sroot = troot / "sessions"
    for sdir in sorted(sroot.iterdir()) if sroot.is_dir() else []:
        if not sdir.is_dir():
            continue
        st = sdir / "session.json"
        state = json.loads(st.read_text(encoding="utf-8")) if st.is_file() else {}
        out.append({"ts": state.get("imported_at") or "", "dir": sdir,
                    "state": state, "size": _du([sdir])})
    return out


def sessions(action="list", keep=None, tenant=None):
    """【服务端】多租户总览 / prune（按租户保留最近 N 个会话，连带清 data 树产物）。"""
    cfg = load_config()
    root = Path(cfg["server"]["remote_root"])
    troot_all = root / "tenants"
    tenants = [d for d in sorted(troot_all.iterdir())
               if d.is_dir()] if troot_all.is_dir() else []
    if not tenants:
        print(f"（空）{troot_all} 下暂无租户")
        return

    if action == "list":
        for t in tenants:
            entries = _tenant_sessions(t)
            data_size = _du([t / "data"])
            print(f"◇ 租户 {t.name}（data 树 {data_size/1e9:.2f}GB，"
                  f"{len(entries)} 个会话）")
            if not entries:
                print("    （暂无会话）")
            for e in entries:
                vr = e["state"].get("video_ready")
                print(f"  {e['ts'][:19]:<20}{'源片✓' if vr else '仅评估包':<9}"
                      f"{e['size']/1e9:>8.2f}GB  {e['dir'].name}")
        return

    if action == "prune":
        keep = keep or int(cfg["server"].get("keep_sessions", 3))
        targets = [t for t in tenants if tenant in (None, t.name)]
        if tenant and not targets:
            raise ValueError(f"租户不存在: {tenant}（可用: {[t.name for t in tenants]}）")
        for t in targets:
            data_root = t / "data"
            entries = sorted(_tenant_sessions(t), key=lambda e: e["ts"])
            victims = entries[:-keep] if len(entries) > keep else []
            if not victims:
                print(f"[{t.name}] {len(entries)} 个会话 <= 保留 {keep}，无需 prune")
                continue
            for e in victims:
                doomed = [e["dir"]]
                for p in e["state"].get("parts", []):
                    if p.get("asr_dir"):
                        doomed.append(data_root / "asr" / p["asr_dir"])
                    if p.get("events_rpt"):
                        doomed.append(data_root / "reports" / p["events_rpt"])
                lines = e["state"].get("lines", {})
                if lines.get("knockdowns"):
                    doomed.append(data_root / "knockdowns" / lines["knockdowns"])
                if lines.get("pickups"):
                    doomed.append(data_root / "pickups" / lines["pickups"])
                for vd in lines.get("voice") or []:
                    doomed.append(data_root / "voice" / vd)
                for d in doomed:
                    if d and d.is_dir():
                        shutil.rmtree(d)
                print(f"[{t.name}] 已删 sessions/{e['dir'].name} "
                      f"及 {len(doomed)-1} 处 data 产物")
        print(f"prune 完成：每租户保留最近 {keep} 个会话")
