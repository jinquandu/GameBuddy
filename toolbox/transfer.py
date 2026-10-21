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
from fnmatch import fnmatch
from pathlib import Path

from toolbox.common import collect_videos, du, get_logger, slug
from toolbox.config import DATA, REMIXES, REPORTS, load_config

log = get_logger("transfer")

# 子进程不闪控制台窗：Windows 用 CREATE_NO_WINDOW，服务端（Linux）恒 0、
# 等价无操作。proc.py 是端侧独有模块，同源文件内联此常量保双端可跑
# （REQ-20261014-004）。
NO_WINDOW = 0x08000000 if os.name == "nt" else 0

# 端侧<->服务端契约版本：与 docs/SERVER-INTERFACE.md 同步维护（先改文档再改
# 此值）；写入 manifest.json 供服务端检测端侧契约版本。分级见契约文档 §0。
# 1.3.0：受控 HTTPS 上传通道 C2S-6~9（Bearer 租户 token、路径白名单、
# 分块续传、complete 自动 import；REQ-20261019-005）；
# 1.2.0：events.jsonl kind=loadout 补 meta.hero/weapons（REQ-20261014-002）；
# 1.1.2/1.1.3 为逐场报告线补录与 site_url 迁移（2026-10-14 并轨编号，见 §8）。
CONTRACT_VERSION = "1.3.0"


# ---------------------------------------------------------------- 会话发现


def _canonical_rpt(video: Path):
    """事件流在服务端的规范目录名：<分片stem>_full（与 knockdown 链路自检写出的
    命名一致；事件池 discover_pool 只认 *_full，本地历史裸名目录在搬运时归一）。"""
    return slug(video.stem) + "_full"


def _find_events_rpt(video: Path):
    """找该分片的 events.jsonl 报告目录。规范 _full 目录存在即结论——
    **空文件也是结论**（0 事件；原子写后文件存在即完整。2026-10-15 实测
    空报告曾被登记成「无事件流」，manifest 误标 incomplete）；规范路径
    不存在或首行损坏再按 src 匹配：其他 _full 目录 > 任意目录（历史裸名，
    可能只含小扫描子集）。与 events.existing_events 同口径。"""
    canonical = REPORTS / _canonical_rpt(video)
    cfile = canonical / "events.jsonl"
    if cfile.is_file():
        lines = [l for l in cfile.read_text(encoding="utf-8").splitlines()
                 if l.strip()]
        if not lines:
            return canonical
        try:
            json.loads(lines[0])
        except json.JSONDecodeError:
            pass                              # 首行损坏：落回 src 匹配
        else:
            return canonical
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
    if canonical in cands:
        return canonical
    full = [c for c in cands if c.name.endswith("_full")]
    return (full or cands)[0]


def discover_session(session):
    """按各链路既定命名规则聚拢一个场次的所有产物（缺失项为 None/[]）。"""
    # toolbox.asr 为端侧独有模块：懒加载，服务端不调本函数即不触发
    from toolbox.asr import find_transcript_dir
    target = Path(session).expanduser()
    videos = collect_videos(session)
    session_name = (videos[0].parent.name if len(videos) > 1 or target.is_dir()
                    else videos[0].stem)

    parts, voice_dirs, missing, local_rpts = [], [], [], []
    for v in videos:
        asr_dir = find_transcript_dir(v)
        rpt = _find_events_rpt(v)
        # missing 文案会写入 manifest.json 给打标站看，用全名（2026-10-15
        # 实测 [:30] 把长中文名连扩展名一起截掉）；控制台打印截断是纯显示
        # 层，各链路 [:36] 维持原样
        if not asr_dir:
            missing.append(f"{v.name}：无 ASR 转写（先跑 toolbox asr）")
        if not rpt:
            missing.append(f"{v.name}：无事件流（先跑 toolbox detect）")
        entry = {"client_video": str(v), "filename": v.name,
                 "asr_dir": asr_dir.name if asr_dir else None,
                 # 服务端统一落规范 _full 名：事件池/报告只认 *_full
                 "events_rpt": _canonical_rpt(v) if rpt else None}
        voice_dir = DATA / "voice" / slug(asr_dir.name, 40) if asr_dir else None
        if voice_dir and voice_dir.is_dir():
            voice_dirs.append(voice_dir)
            entry["voice_dir"] = voice_dir.name
        parts.append(entry)
        if rpt:
            local_rpts.append(rpt)

    kd_dir = DATA / "knockdowns" / slug(session_name, 60)
    pk_dir = DATA / "pickups" / slug(session_name, 60)
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


# ---------------------------------------------------------------- 进程执行

def _ssh(alias, remote_cmd, check=True):
    """控制通道（mkdir/import 触发）：Windows 固定用原生 OpenSSH（读
    ~/.ssh/config 无障碍）；MSYS ssh 仅供 rsync 进程内使用（见 _rsync 注释），
    PATH 里排在前面时不能让它劫持这里的 "ssh"。输出显式按 UTF-8 解——
    服务端恒 UTF-8，text=True 裸跑会随 locale 走 GBK，中文输出轻则乱码、
    重则 reader 线程 UnicodeDecodeError 后 stdout=None 炸 _trigger
    （2026-10-19 exe 实测：import-session 输出含场次中文名即崩）。"""
    bin_ = "ssh"
    if os.name == "nt":
        native = Path(r"C:\Windows\System32\OpenSSH\ssh.exe")
        if native.exists():
            bin_ = str(native)
    return subprocess.run([bin_, alias, remote_cmd],
                          check=check, text=True,
                          encoding="utf-8", errors="replace",
                          capture_output=True,
                          creationflags=NO_WINDOW)


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
                                   capture_output=True, timeout=10,
                                   creationflags=NO_WINDOW)
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
    # 显式管道而非继承句柄（2026-10-15 exe 实测 P0）：msys rsync 派生 msys ssh
    # 时对继承的标准句柄 dup() 失败（"dup() in/out/err failed" -> exit 12），
    # bash/真实控制台/源码三场景全挂；显式管道句柄则全场景可用。副作用是
    # --progress 不再直达控制台——失败时把 stderr 尾部并入报错保排障信息
    # （windowed exe 里 stderr 本就落在无效句柄，用户原先只能看到裸退出码）。
    r = subprocess.run(cmd, env=env,
                       stdin=subprocess.DEVNULL,
                       stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                       text=True, encoding="utf-8", errors="replace",
                       creationflags=NO_WINDOW)
    if r.returncode != 0:
        tail = "\n".join((r.stderr or "").strip().splitlines()[-3:])
        raise RuntimeError(f"rsync 失败（退出码 {r.returncode}）：{src} -> {dst}"
                           + (f"\n{tail}" if tail else ""))


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
    # _scancache/_lobbycache 是端侧小扫描断点缓存（服务端可重建，不上送；
    # _lobbycache 为 2026-09-22 补漏——此前已实际上送违反契约 §5 排除原则）
    "pickups": ("l2/", "_frames/", "review.html", "scores.json",
                "_scancache.json", "_lobbycache.json"),
    "voice": ("review.html",),
}


def _auto_tenant_id():
    """装机时间坐标租户 ID：auto-YYYYMMDD-HHMMSS（_tenant_root 合法字符集内）。"""
    return "auto-" + datetime.now().strftime("%Y%m%d-%H%M%S")


def _persist_tenant(tenant):
    """把 tenant 写入 ROOT/config.yaml，保留其余内容与注释（BOM 顺手归一）。

    三种落点：已有 tenant 行 → 原位换值；有 server: 节 → 插到节首；
    都没有（甚至无 config.yaml）→ 追加最小 server 节。"""
    from toolbox.config import ROOT
    path = ROOT / "config.yaml"
    quoted = f'"{tenant}"'      # 引号防 YAML 把 0 开头/纯数字串当数值或八进制
    if path.exists():
        text = path.read_text(encoding="utf-8-sig")
        # 行内空白用 [ \t]：\s 会吞换行，把替换值挤到下一行
        if re.search(r"^[ \t]*tenant[ \t]*:", text, flags=re.M):
            text = re.sub(r"^([ \t]*tenant[ \t]*:)[ \t]*.*$", r"\g<1> " + quoted,
                          text, count=1, flags=re.M)
        elif re.search(r"^server[ \t]*:", text, flags=re.M):
            if not text.endswith("\n"):     # server: 为末行无换行时插不进
                text += "\n"
            text = re.sub(r"^(server[ \t]*:)[ \t]*\n",
                          "\\1\n" + f"  tenant: {quoted}\n",
                          text, count=1, flags=re.M)
        else:
            text = text.rstrip("\n") + f"\n\nserver:\n  tenant: {quoted}\n"
        path.write_text(text, encoding="utf-8")
    else:
        path.write_text(f"server:\n  tenant: {quoted}\n", encoding="utf-8")


def _ensure_tenant(cfg):
    """tenant 未配置时按当前时间生成本机租户 ID 并写回 config.yaml。

    分发包 config 不含 tenant（2026-10-15 实测 P2-2：打包机租户外泄会
    串号），若缺省都落到字面 "default"，不同客户的数据会混进同一租户——
    改为装机首次使用时刻生成 auto-* 并持久化，本机后续 push/pull 复用
    同一租户。用户显式配置（config.yaml / CLI --tenant）优先，不受影响。
    判断必须看原始用户配置（config.user_server_cfg）：合并结果恒含
    DEFAULTS 的 "default"，分不出「没配」和「配了 default」。"""
    from toolbox.config import user_server_cfg
    user_t = user_server_cfg().get("tenant")
    if user_t is not None:
        return user_t
    tenant = _auto_tenant_id()
    cfg["tenant"] = tenant          # 本进程内立即生效（cfg 是 load_config 的节点）
    try:
        _persist_tenant(tenant)
        print(f"未配置租户：已按本机时间生成 tenant={tenant} 并写入 config.yaml"
              "（归入指定租户请改 server.tenant 后重推）")
    except OSError as e:
        print(f"警告：租户 {tenant} 写回 config.yaml 失败（{e}），"
              "下次运行将生成新租户，请尽快显式配置 server.tenant")
    return tenant


def _tenant_root(cfg, tenant=None):
    """多租户方案A：数据按 /opt/dashijie-eval-data/tenants/<租户>/ 分前缀。
    tenant 未配置时自动分配（见 _ensure_tenant）；CLI --tenant 显式覆盖。"""
    raw = tenant if tenant is not None else _ensure_tenant(cfg)
    t = str(raw).strip()   # YAML 会把 007 解析成 int，统一转字符串
    if not t or not re.match(r"^[A-Za-z0-9_-]{1,32}$", t):
        raise ValueError(f"租户名不合法: {t!r}（仅限字母数字-_，≤32 字）")
    return f"{cfg['remote_root']}/tenants/{t}"


def push(session, mode="eval", dry=False, run_eval=True, no_import=False,
         tenant=None):
    """上传会话产物到服务端。mode: eval=评估包（默认）| video=成片包（源片）。

    双通道（契约 1.3.0）：config server.upload_url 非空 → 受控 HTTPS 上传
    （客户机；Bearer token 鉴权，tenant 由服务端解出，complete 自动 import）；
    为空 → rsync over ssh 直达服务端 data/ 工作树（内部机），两种模式完成后
    （除非 --no-import）均触发容器内 import-session 路径重映射+状态登记。
    run_eval 自 2026-10-21（REQ-20261021-002）**评估包默认开启**——mode=eval
    的 push 等价 --run-eval（complete run_eval:true），--no-run-eval 显式关；
    成片包（video）恒不触发评估。多租户：rsync 通道 --tenant 覆盖
    server.tenant 配置；HTTPS 通道 tenant 只由 token 决定。
    """
    run_eval = bool(run_eval) and mode == "eval"   # video 恒不评（双通道统一门）
    cfg = load_config()["server"]
    if cfg.get("upload_url"):
        if not cfg.get("upload_token"):
            # 账号模式上传（REQ-003 拆解 3.6，用户 2026-10-20 批准方案②）：
            # 无租户 token → 用登录会话 Bearer + DSJ-PC-1 签名头，服务端
            # 经 INT-1 核验并解出 usr_* 租户——客户机零配置 token
            from toolbox.account import require_login
            try:
                require_login()
            except Exception as e:          # noqa: BLE001 未登录/未配账号服务
                raise RuntimeError(
                    "HTTPS 上传未配置 upload_token（账号模式），需先在桌宠"
                    f"界面登录：{e}") from None
        return _push_https(cfg, session, mode, dry, run_eval, no_import, tenant)
    alias, remote = cfg["ssh_alias"], _tenant_root(cfg, tenant)
    disc = discover_session(session)

    if mode == "video":
        vids = [Path(p["client_video"]) for p in disc["parts"]]
        if not vids or not all(v.is_file() for v in vids):
            raise RuntimeError(f"源片不存在：{session}")
        total = du(vids)
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
        total = du([m[0] for m in mappings])
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


# ---------------------------------------------------------------- HTTPS 通道

# rsync --exclude 语义的枚举等价：尾斜杠=任意层目录段名，否则 basename 通配
def _excluded(rel_posix, excludes):
    segs = rel_posix.split("/")
    for pat in excludes:
        if pat.endswith("/"):
            if pat[:-1] in segs:
                return True
        elif fnmatch(segs[-1], pat):
            return True
    return False


def _plan_files(disc, mode):
    """把一次 push 的内容枚举成 HTTPS 上传清单：返回 (files, local_map,
    manifest)。files=[(租户根相对路径, size, sha256)] 只含数据/源片文件；
    manifest 走 complete 请求体由服务端落盘（重跑时 pushed_at 变化不破坏
    清单签名，断点续传才成立）。路径白名单与 rsync 通道同一批目标
    （§C2S-1/C2S-2），排除规则同 EVAL_EXCLUDES。"""
    from toolbox.uploader import file_sha256
    files, local_map = [], {}

    def add_file(local: Path, rel: str):
        files.append([rel, local.stat().st_size, file_sha256(local)])
        local_map[rel] = local

    def add_dir(local: Path, rel_dir: str, excludes=()):
        for p in sorted(local.rglob("*")):
            if not p.is_file():
                continue
            rel = p.relative_to(local).as_posix()
            if _excluded(rel, excludes):
                continue
            add_file(p, f"{rel_dir}/{rel}")

    if mode == "video":
        vids = [Path(p["client_video"]) for p in disc["parts"]]
        if not vids or not all(v.is_file() for v in vids):
            raise RuntimeError(f"源片不存在：{disc['session']}")
        for v in vids:
            add_file(v, f"sessions/{disc['session']}/video/{v.name}")
    else:
        for d in disc["asr_dirs"]:
            add_dir(d, f"data/asr/{d.name}", EVAL_EXCLUDES["asr"])
        parts_with_rpt = [p for p in disc["parts"] if p.get("events_rpt")]
        for r, p in zip(disc["events_rpts_local"], parts_with_rpt):
            add_dir(r, f"data/reports/{p['events_rpt']}")
        if disc["knockdowns_dir"]:
            add_dir(disc["knockdowns_dir"],
                    f"data/knockdowns/{disc['knockdowns_dir'].name}",
                    EVAL_EXCLUDES["knockdowns"])
        if disc["pickups_dir"]:
            add_dir(disc["pickups_dir"],
                    f"data/pickups/{disc['pickups_dir'].name}",
                    EVAL_EXCLUDES["pickups"])
        for v in disc["voice_dirs"]:
            add_dir(v, f"data/voice/{v.name}", EVAL_EXCLUDES["voice"])
        if not files:
            raise RuntimeError("没有可上传的产物")

    return files, local_map, _build_manifest(disc, mode)


def _push_https(cfg, session, mode, dry, run_eval, no_import, tenant_override):
    """受控 HTTPS push（契约 C2S-6~9）。tenant 由服务端从 token 解出。"""
    if tenant_override:
        print(f"HTTPS 通道：tenant 由服务端从上传 token 解出，"
              f"--tenant {tenant_override} 忽略")
    disc = discover_session(session)
    if mode != "video" and disc["missing"]:
        print("警告：以下产物缺失，将如实登记为 incomplete：")
        for m in disc["missing"]:
            print(f"  - {m}")
    files, local_map, manifest = _plan_files(disc, mode)
    total = sum(s for _, s, _ in files)
    print(f"{'成片包' if mode == 'video' else '评估包'}（HTTPS）："
          f"{len(files)} 个文件，约 {total / 1e9:.2f}GB")
    for rel, size, _ in files[:5]:
        print(f"  {rel}（{size / 1e6:.1f}MB）")
    if len(files) > 5:
        print(f"  … 共 {len(files)} 个")
    if dry:
        print("（dry-run：未连接服务端）")
        return
    from toolbox.uploader import ProductionUploader, push_files
    if cfg.get("upload_token"):
        uploader = ProductionUploader(cfg["upload_url"], cfg["upload_token"])
    else:
        # 账号模式：Bearer=登录会话 token，签名头带同一 bearer（DSJ-PC-1
        # 规范化串含 bearer 摘要，与 account 客户端同口径）
        from toolbox import device_identity as _di
        from toolbox.account import require_login
        _sess = require_login()
        _b = _sess["bearer"]
        uploader = ProductionUploader(
            cfg["upload_url"], token=_b,
            signer=lambda m, w, d: _di.sign_request(m, w, d, _b))
    receipt = push_files(
        uploader,
        disc["session"], mode, [tuple(f) for f in files], local_map,
        manifest, run_eval=run_eval, no_import=no_import)
    state = receipt.get("state")
    print(f"服务端回执：state={state} session={receipt.get('session')} "
          f"tenant={receipt.get('tenant')} "
          f"remapped={receipt.get('remapped')} "
          f"video_ready={receipt.get('video_ready')}")
    if receipt.get("log"):
        print(receipt["log"][-600:])
    if state != "imported":
        raise RuntimeError(
            f"上传完成但 import 未成功（state={state}）；"
            "数据已在服务端，重跑本命令可重试 import")


def _build_manifest(disc, mode):
    """manifest.json 内容（rsync 与 HTTPS 两通道共用，schema 见契约 §5.2）。"""
    return {
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


def _write_manifest(alias, remote, disc, mode, dry=False):
    manifest = _build_manifest(disc, mode)
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
    if r.stdout and r.stdout.strip():   # None 防卫：读端异常时不在此二次崩
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
    log.info("import_session 开始 session=%r remote_root=%s", session, root)
    if not (sdir / "manifest.json").is_file():
        raise FileNotFoundError(f"无 manifest：{sdir}/manifest.json（先在应用端 push）")
    manifest = json.loads((sdir / "manifest.json").read_text(encoding="utf-8"))
    # §7：服务端忽略 manifest 未知字段；契约主版本未知则提示（不阻断）。
    mv = str(manifest.get("contract_version", ""))
    if mv.split(".")[0] != CONTRACT_VERSION.split(".")[0]:
        log.warning("契约主版本不同 端侧=%s 服务端=%s session=%r",
                    mv or "(缺省)", CONTRACT_VERSION, session)
        print(f"[警告] 端侧契约版本 {mv or '(缺省)'} 与服务端 {CONTRACT_VERSION} 主版本不同，"
              "请确认双端是否同步发版")

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
                try:
                    e = json.loads(l)
                except json.JSONDecodeError:   # 历史截断尾行：跳过，不让 push 炸
                    print(f"  警告：{rpt.name} 损坏行跳过（src 重映射）")
                    continue
                if e.get("src") == client_video:
                    e["src"] = server_video
                    remapped += 1
                else:
                    unchanged += 1
                out.append(json.dumps(e, ensure_ascii=False))
            tmp = rpt / "events.jsonl.tmp"   # 原子替换，中途被杀不留截断
            tmp.write_text("\n".join(out) + "\n", encoding="utf-8")
            os.replace(tmp, rpt / "events.jsonl")

    state_p = sdir / "session.json"
    state = json.loads(state_p.read_text(encoding="utf-8")) if state_p.is_file() else {}
    # §5.3：session.json 是 manifest 的超集 —— 先继承 manifest 全部字段
    # （contract_version / pushed_at / missing 等），再覆盖服务端状态字段。
    # lines.report 是服务端状态（SRV-2 eval 写，manifest 不含），幂等重跑
    # 不得抹掉（契约 1.1.2）—— 须在 update(manifest) 覆盖 lines 前留存。
    old_report = (state.get("lines") or {}).get("report")
    state.update(manifest)
    lines = dict(manifest.get("lines") or {})
    if old_report and "report" not in lines:
        lines["report"] = old_report
    state.update({
        "session": session,
        "imported_at": datetime.now().isoformat(timespec="seconds"),
        "mode": manifest.get("mode"),
        "video_ready": all((sdir / "video" / p["filename"]).is_file()
                           for p in manifest["parts"]),
        "parts": [dict(p, server_video=str(sdir / "video" / p["filename"]))
                  for p in manifest["parts"]],
        "lines": lines,
    })
    state_p.write_text(json.dumps(state, ensure_ascii=False, indent=2),
                       encoding="utf-8")
    print(f"[{session}] 重映射 {remapped} 处 / 未匹配 {unchanged}；"
          f"video_ready={state['video_ready']} -> {state_p}")
    log.info("import_session 完成 session=%r remapped=%d unchanged=%d "
             "video_ready=%s", session, remapped, unchanged,
             state["video_ready"])


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
                    "state": state, "size": du(sdir)})
    return out


def sessions(action="list", keep=None, tenant=None):
    """【服务端】多租户总览 / prune（按租户保留最近 N 个会话，连带清 data 树产物）。

    root 解析优先级：TOOLBOX_REMOTE_ROOT 环境变量 > config server.remote_root
    （前者是服务端本地测试/运维便利，不改变端侧契约）。"""
    cfg = load_config()
    root = Path(os.environ.get("TOOLBOX_REMOTE_ROOT")
                or cfg["server"]["remote_root"])
    troot_all = root / "tenants"
    tenants = [d for d in sorted(troot_all.iterdir())
               if d.is_dir()] if troot_all.is_dir() else []
    if not tenants:
        print(f"（空）{troot_all} 下暂无租户")
        return

    if action == "list":
        for t in tenants:
            entries = _tenant_sessions(t)
            data_size = du(t / "data")
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
                if lines.get("report"):
                    doomed.append(data_root / "game_reports" / lines["report"])
                for vd in lines.get("voice") or []:
                    doomed.append(data_root / "voice" / vd)
                for d in doomed:
                    if d and d.is_dir():
                        shutil.rmtree(d)
                print(f"[{t.name}] 已删 sessions/{e['dir'].name} "
                      f"及 {len(doomed)-1} 处 data 产物")
                # 删除审计：prune 不可逆，留痕每个被删目录（不可重建的会话状态在列）
                log.info("prune 删除 tenant=%s session=%r imported_at=%s "
                         "dirs=%s", t.name, e["dir"].name, e["ts"] or "?",
                         [str(d.relative_to(t)) for d in doomed if d])
        print(f"prune 完成：每租户保留最近 {keep} 个会话")
        log.info("prune 完成 keep=%d tenants=%s", keep,
                 [t.name for t in targets])
