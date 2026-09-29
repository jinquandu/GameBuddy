"""统一命令行入口：python3 -m toolbox <子命令>。

【应用端】record / download / asr / speaker / detect / knockdowns / pickups /
          voice / pipeline / push / pull
【服务端】import-session / eval / sessions（容器内执行，接口契约见
          docs/SERVER-INTERFACE.md）
【本地通用】ingest / list / highlight / remix / score / pscore / report / stats
"""
import argparse
import sys

from toolbox import __version__, ingest as ingest_mod
from toolbox.config import ensure_dirs, load_config


def cmd_download(args):
    from toolbox.download import download
    download(args.url, parts=args.parts, out_dir=args.out)


def cmd_record(args):
    from toolbox.recorder import record
    record(args.target, once=args.once, qn=args.qn, out_dir=args.out,
           segment_min=args.segment_min, stall_sec=args.stall_sec,
           probe_only=args.probe)


def cmd_asr(args):
    from toolbox.asr import asr
    asr(args.target)


def cmd_push(args):
    from toolbox.transfer import push
    push(args.session, mode=args.mode, dry=args.dry_run,
         run_eval=args.run_eval, no_import=args.no_import, tenant=args.tenant)


def cmd_pull(args):
    from toolbox.transfer import pull
    pull(template=args.template, dry=args.dry_run, tenant=args.tenant)


def cmd_import_session(args):
    from toolbox.transfer import import_session
    import_session(args.session, remote_root=args.remote_root)


def cmd_sessions(args):
    from toolbox.transfer import sessions
    sessions(action=args.action, keep=args.keep, tenant=args.tenant)


def cmd_eval(args):
    from toolbox.eval import eval_session
    eval_session(args.session, no_html=args.no_html,
                 remote_root=args.remote_root)


def cmd_ingest(args):
    for f in args.files:
        ingest_mod.ingest_file(f, game=args.game, source=args.source)


def cmd_list(args):
    rows = ingest_mod.list_recordings()
    if not rows:
        print("（空）还没有登记视频，先运行: python3 -m toolbox ingest <视频路径>")
        return
    print(f"{'ID':<6}{'时长(s)':>9}  {'分辨率':<11}{'游戏':<8}{'来源':<15}文件")
    for r in rows:
        res = f"{r['width']}x{r['height']}"
        print(f"{r['id']:<6}{r['duration_sec']:>9}  {res:<11}"
              f"{(r['game'] or '-'):<8}{r['source']:<15}{r['file']}")


def cmd_highlight(args):
    from toolbox.highlight import cut_highlights, detect_highlights, resolve_source
    resolve_source(args.video_id)              # 校验：登记 id 或文件路径
    cfg = load_config()
    cut_highlights(args.video_id, detect_highlights(args.video_id, cfg), cfg)


def cmd_remix(args):
    from toolbox.config import TEMPLATES_DIR
    from toolbox.remix import remix
    name = args.template or load_config()["remix"]["template"]
    names = (sorted(p.stem for p in TEMPLATES_DIR.glob("*.yaml"))
             if name == "all" else [name])
    for n in names:
        if n == "default":
            continue    # 旧示例模板，无对应策略
        remix(n)


def cmd_publish(args):
    from toolbox.douyin import publish
    publish(args.file, title=args.title, tags=args.tags, config=load_config())


def cmd_feedback(args):
    from toolbox.douyin import fetch_comments, summarize_feedback
    cfg = load_config()
    summarize_feedback(fetch_comments(args.video_id, cfg), cfg)


def cmd_stats(args):
    from toolbox.stats import extract_stats
    path = ingest_mod.recording_path(args.video_id)
    extract_stats(path, load_config())


def cmd_knockdowns(args):
    from toolbox.knockdown import knockdowns
    for target in args.targets:
        knockdowns(target, load_config(), pre=args.pre, post=args.post,
                   merge=args.merge, redetect=args.redetect, fps=args.fps,
                   workers=args.workers)


def cmd_score(args):
    from toolbox import score as score_mod
    for target in args.targets:
        score_mod.score_session(target, load_config(), frames=args.frames,
                                skip_l1=args.skip_l1)
    if args.html:
        score_mod.render_html()


def cmd_pickups(args):
    from toolbox.pickup import pickups
    for target in args.targets:
        pickups(target, load_config(), merge=args.merge,
                redetect=args.redetect, fps=args.fps, workers=args.workers)


def cmd_pscore(args):
    from toolbox import pickup_score as pscore_mod
    for target in args.targets:
        pscore_mod.score_session(target, load_config(), frames=args.frames,
                                 skip_l1=args.skip_l1)
    if args.html:
        pscore_mod.render_html()


def cmd_report(args):
    import re as _re
    from pathlib import Path as _P
    from toolbox.chrono import build_matches
    from toolbox.remix import discover_pool
    from toolbox.report import render_report
    pool = discover_pool()
    matches = build_matches(pool)
    if not matches:
        raise RuntimeError("事件池为空：先跑 detect 产出 data/reports/*_full/events.jsonl")
    name = ""
    m = _re.search(r"(\d{4})年(\d{2})月(\d{2})日", _P(next(iter(pool["videos"]), "")).name)
    if m:
        name = f"game_report_{m.group(1)}{m.group(2)}{m.group(3)}.md"
    md, out = render_report(matches, out_name=name)
    print(md)
    print(f"\n报告已写入: {out}")


def cmd_pipeline(args):
    from toolbox.pipeline import run_pipeline
    run_pipeline(args.target, load_config(), skip=set(args.skip),
                 run_eval=args.run_eval)


def cmd_pet(args):
    try:
        from toolbox.pet import run
    except ImportError as e:
        raise RuntimeError(
            f"宠物客户端需要 tkinter（本 Python 缺失：{e}）。"
            "用 python.org 官方安装包重装/修复 Python，或换完整版解释器。") from e
    run(address=args.address)


def cmd_screenrec(args):
    from toolbox.screenrec import record
    record(out_dir=args.out, cfg={"segment_min": args.segment_min,
                                  "mic": args.mic})


def cmd_detect(args):
    from toolbox import detector
    from toolbox.config import REPORTS
    from pathlib import Path as _P
    if args.video_id.startswith("v") and args.video_id[1:].isdigit():
        src = ingest_mod.recording_path(args.video_id)   # 已登记视频
        name = args.video_id
    else:
        src = _P(args.video_id).expanduser().resolve()   # 直接给文件路径
        name = src.stem[:40]
    out = _P(args.out) if args.out else REPORTS / name / "events.jsonl"
    out.parent.mkdir(parents=True, exist_ok=True)

    def prog(t):
        print(f"  ... scanned {t:.0f}s", flush=True)

    transcript = None
    if args.transcript:
        import json as _json
        tpath = _P(args.transcript)
        # 同目录若有说话人标注版（toolbox speaker 产出）则优先用——语音
        # 交叉只认主播句，避免 TTS 播报词误触发估价
        labeled = tpath.parent / "speaker_labeled.json"
        use = labeled if labeled.exists() else tpath
        tdata = _json.loads(use.read_text(encoding="utf-8"))
        transcript = tdata.get("sentences", tdata)   # 兼容 {"sentences":[...]} 与裸列表
    events = detector.scan_video(src, fps=args.fps,
                                 config=load_config(),
                                 progress=prog if args.verbose else None,
                                 transcript=transcript,
                                 workers=args.workers)
    detector.write_jsonl(events, out, src=str(src))
    print(f"{detector.summarize(events)}")
    print(f"事件流已写入: {out}")


def cmd_speaker(args):
    from toolbox import speaker
    out, summary = speaker.run(args.transcript, audio_path=args.audio,
                               video_path=args.video,
                               threshold=args.threshold)
    print(f"聚类 {summary['clusters']} 簇 | 主播 {summary['host_sec']:.0f}s / "
          f"其他 {summary.get('other_sec', 0):.0f}s")
    print(f"已写出: {out}")


def cmd_voice(args):
    from toolbox.voice import render_html, voice_session
    for target in args.targets:
        voice_session(target, load_config(), top=args.top, force=args.force)
    if args.html:
        render_html()


def build_parser():
    p = argparse.ArgumentParser(
        prog="toolbox", description="陪玩助手工具箱：录屏→高光→混剪→发布→反馈 / 统计→报告")
    p.add_argument("-V", "--version", action="version",
                   version=f"toolbox {__version__}")
    sub = p.add_subparsers(dest="cmd", required=True, metavar="<子命令>")

    sp = sub.add_parser(
        "record",
        help="【应用端】B站直播录制：CDN 直流转封装零重编码（主播推 1080P 即无损 1080P），"
             "自动分片+断流重连+下播守护，产物直接进场次契约")
    sp.add_argument("target", help="直播间 URL 或房间号（纯数字）")
    sp.add_argument("--once", action="store_true",
                    help="只录本场（下播即退出）；缺省 watch 守护，下播后等开播续录")
    sp.add_argument("--qn", type=int, default=None,
                    help="画质档（默认取配置 recorder.qn=10000 原画1080P；"
                         "房间不支持时自动降最高可用档）")
    sp.add_argument("--out", default=None, help="输出目录（默认 video/<房间标题>）")
    sp.add_argument("--segment-min", type=float, default=None,
                    help="单分片分钟数（默认 30，到点轮转，P 号延续）")
    sp.add_argument("--stall-sec", type=float, default=None,
                    help="无数据判定断流秒数（默认 20，超时重拉直链续录）")
    sp.add_argument("--probe", action="store_true",
                    help="只探测房间状态与实际画质，不录制")
    sp.set_defaults(func=cmd_record)

    sp = sub.add_parser(
        "screenrec",
        help="【应用端】桌面录制：屏幕+系统声音（soundcard loopback），"
             "按日期产出场次目录/分P，可直接 pipeline")
    sp.add_argument("--out", default=None,
                    help="录像根目录（默认 video/，1级=日期场次目录，2级=分P）")
    sp.add_argument("--segment-min", type=float, default=None,
                    help="单分P分钟数（默认取配置 screenrec.segment_min=15）")
    sp.add_argument("--mic", action="store_true",
                    help="把默认麦克风混录进音轨（默认只录系统声音）")
    sp.set_defaults(func=cmd_screenrec)

    sp = sub.add_parser(
        "download", help="【应用端】B站视频/分P下载（1080P avc1，断点续传，产出场次目录）")
    sp.add_argument("url", help="BV 号或视频页 URL")
    sp.add_argument("--parts", default="all",
                    help="分 P 选择：all（默认）/ 1,2,3 / 1-3")
    sp.add_argument("--out", default=None, help="输出目录（默认 video/<标题>）")
    sp.set_defaults(func=cmd_download)

    sp = sub.add_parser(
        "asr", help="【应用端】ASR 转写：视频/场次目录 -> data/asr/<场次>/transcript.json（逐句时间戳）")
    sp.add_argument("target", help="视频文件或场次目录（含 P1..PN 分片）")
    sp.set_defaults(func=cmd_asr)

    sp = sub.add_parser(
        "push", help="【应用端】上传会话产物到服务端（默认评估包；--mode video 补传源片）")
    sp.add_argument("session", help="场次目录（video/<场次>）或视频文件")
    sp.add_argument("--mode", choices=["eval", "video"], default="eval",
                    help="eval=评估包：JSON+三线切片+L1缓存（默认）；"
                         "video=成片包：源片全量（~4-5G，确认做片才推）")
    sp.add_argument("--run-eval", action="store_true",
                    help="上传后触发服务端评估链（打分+报告+review 页）")
    sp.add_argument("--no-import", action="store_true",
                    help="不触发服务端 import-session（调试用）")
    sp.add_argument("--tenant", default=None,
                    help="租户（多租户方案A）；缺省取 server.tenant 配置")
    sp.add_argument("-n", "--dry-run", action="store_true", help="只列不传")
    sp.set_defaults(func=cmd_push)

    sp = sub.add_parser(
        "pull", help="【应用端】下载服务端 remix 成片+发布包到本地 data/remixes/")
    sp.add_argument("--template", default=None,
                    help="模板名；缺省拉回服务端全部模板产物")
    sp.add_argument("--tenant", default=None,
                    help="租户；缺省取 server.tenant 配置")
    sp.add_argument("-n", "--dry-run", action="store_true", help="只列不拉")
    sp.set_defaults(func=cmd_pull)

    sp = sub.add_parser(
        "import-session", help="【服务端】push 后收口：路径重映射+会话状态登记（幂等）")
    sp.add_argument("session", help="场次名（sessions/ 下目录名）")
    sp.add_argument("--remote-root", default=None,
                    help="租户根（默认取 server.remote_root 配置）")
    sp.set_defaults(func=cmd_import_session)

    sp = sub.add_parser(
        "sessions", help="【服务端】多租户总览（列表/占用）/ prune（每租户保留最近 3 个）")
    sp.add_argument("action", nargs="?", choices=["list", "prune"], default="list")
    sp.add_argument("--keep", type=int, default=None, help="prune 保留数（默认 3）")
    sp.add_argument("--tenant", default=None, help="只作用于该租户（缺省=全部租户）")
    sp.set_defaults(func=cmd_sessions)

    sp = sub.add_parser(
        "eval", help="【服务端】评估链：击倒/拾取打分（L2 人工标注合并）+ 逐局报告 + review 页")
    sp.add_argument("session", help="场次名")
    sp.add_argument("--no-html", action="store_true", help="不重生成 review.html")
    sp.add_argument("--remote-root", default=None,
                    help="租户根（多租户下由 push/容器触发时传入）")
    sp.set_defaults(func=cmd_eval)

    sp = sub.add_parser("ingest", help="导入视频并登记元数据（ffprobe）")
    sp.add_argument("files", nargs="+", help="视频文件路径（可多个）")
    sp.add_argument("--game", default=None, help="游戏标识，如 pubg")
    sp.add_argument("--source", default="download",
                    choices=["download", "screen_record"],
                    help="来源：download=下载视频（当前默认）；screen_record=录屏（后续）")
    sp.set_defaults(func=cmd_ingest)

    sp = sub.add_parser("list", help="列出已登记视频")
    sp.set_defaults(func=cmd_list)

    sp = sub.add_parser("highlight", help="检测并裁剪高光片段（ASR 关键词 / LLM 选段）")
    sp.add_argument("video_id", help="已登记视频 id 或视频文件路径")
    sp.set_defaults(func=cmd_highlight)

    sp = sub.add_parser("remix", help="按模板混剪事件流成竖屏短视频")
    sp.add_argument("template", nargs="?", default=None,
                    help="模板名（fast_cut/hot_kills/loot_run/match_story/"
                         "category_mix/single_best），或 all 渲染全部；"
                         "缺省取配置 remix.template")
    sp.set_defaults(func=cmd_remix)

    sp = sub.add_parser("publish", help="上传发布到抖音（已放弃自动实现，发布包手动上传）")
    sp.add_argument("file", help="成片路径")
    sp.add_argument("--title", default="", help="标题")
    sp.add_argument("--tags", default="", help="标签，逗号分隔")
    sp.set_defaults(func=cmd_publish)

    sp = sub.add_parser("feedback", help="拉取评论并总结反馈（骨架）")
    sp.add_argument("video_id", help="已发布视频的平台 id")
    sp.set_defaults(func=cmd_feedback)

    sp = sub.add_parser("stats", help="统计击杀/精准度等指标（骨架）")
    sp.add_argument("video_id")
    sp.set_defaults(func=cmd_stats)

    sp = sub.add_parser("detect", help="逐帧行为识别：进入对局/入局装备/击倒/拾取/撤离 -> 事件流 JSONL")
    sp.add_argument("video_id", help="已登记视频 id 或视频文件路径")
    sp.add_argument("--fps", type=float, default=1.0, help="采样帧率（默认 1）")
    sp.add_argument("--out", default=None, help="输出 JSONL 路径")
    sp.add_argument("--transcript", default=None,
                    help="ASR 转写 json（如 data/asr/p3/transcript.json），"
                         "用于拾取估价的语音交叉")
    sp.add_argument("--workers", type=int, default=None,
                    help="并行进程数（分段并行；默认自动：>=5min 分片 "
                         "min(10, 核数/2)，短分片串行；限每进程 2 OCR 线程)")
    sp.add_argument("-v", "--verbose", action="store_true", help="打印进度")
    sp.set_defaults(func=cmd_detect)

    sp = sub.add_parser("speaker", help="说话人标注：transcript 句子区分主播/播报TTS")
    sp.add_argument("transcript", help="transcript.json 路径")
    sp.add_argument("--audio", default=None, help="16k wav（默认同目录 audio_16k.wav）")
    sp.add_argument("--video", default=None, help="无音频时从此视频提取")
    sp.add_argument("--threshold", type=float, default=0.65,
                    help="聚类余弦距离阈值（默认 0.65）")
    sp.set_defaults(func=cmd_speaker)

    sp = sub.add_parser(
        "voice", help="主播语音情绪评估：全场粗扫 -> 峰段精算打分 -> 切片")
    sp.add_argument("targets", nargs="*",
                    help="asr 目录（含 transcript.json）或 transcript 路径，可多个；"
                         "缺省配合 --html 仅重新生成汇总页")
    sp.add_argument("--top", type=int, default=30, help="每场次保留片段数（默认 30）")
    sp.add_argument("--force", action="store_true", help="重切已存在的片段")
    sp.add_argument("--html", action="store_true",
                    help="（重新）生成 data/voice/review.html 汇总页")
    sp.set_defaults(func=cmd_voice)

    sp = sub.add_parser(
        "knockdowns",
        help="击倒片段链路：事件提取（复用/自动补跑）-> 裁剪所有击倒片段至一个文件夹")
    sp.add_argument("targets", nargs="+",
                    help="场次目录（含 P1..PN 分片）/ 视频文件 / 已登记 id，可多个")
    sp.add_argument("--pre", type=float, default=None,
                    help="击倒前留白秒数（默认 6）")
    sp.add_argument("--post", type=float, default=None,
                    help="击倒后延续秒数（默认 10）")
    sp.add_argument("--merge", type=float, default=None,
                    help="相邻击倒合并窗口秒数，连杀不切碎（默认 12）")
    sp.add_argument("--redetect", action="store_true",
                    help="忽略已有事件流，全部重新检测")
    sp.add_argument("--fps", type=float, default=1.0, help="检测采样帧率（默认 1）")
    sp.add_argument("--workers", type=int, default=None,
                    help="检测并行进程数（0=自动 CPU-1）")
    sp.set_defaults(func=cmd_knockdowns)

    sp = sub.add_parser(
        "score", help="击倒片段打分：L0 事件流 + L1 血量/音频 + L2 评审合并")
    sp.add_argument("targets", nargs="+",
                    help="场次目录 / 击倒片段目录（含 knockdowns.json），可多个")
    sp.add_argument("--frames", action="store_true",
                    help="同时抽取每片段关键帧到 _frames/（供 L2 评审）")
    sp.add_argument("--skip-l1", action="store_true",
                    help="复用 l1/ 缓存，跳过血量/音频重新提取")
    sp.add_argument("--html", action="store_true",
                    help="（重新）生成 review.html，汇总全部已有 scores.json 场次")
    sp.set_defaults(func=cmd_score)

    sp = sub.add_parser(
        "pickups",
        help="拾取会话链路：界面开合小扫描 -> 按「一次开箱/开背包会话」裁剪")
    sp.add_argument("targets", nargs="+",
                    help="场次目录（含 P1..PN 分片）/ 视频文件 / 已登记 id，可多个")
    sp.add_argument("--merge", type=float, default=None,
                    help="粗簇窗口秒数（默认 25），仅用于圈定小扫描范围")
    sp.add_argument("--redetect", action="store_true",
                    help="忽略已有事件流，全部重新检测")
    sp.add_argument("--fps", type=float, default=1.0, help="检测采样帧率（默认 1）")
    sp.add_argument("--workers", type=int, default=None,
                    help="检测并行进程数（0=自动 CPU-1）")
    sp.set_defaults(func=cmd_pickups)

    sp = sub.add_parser(
        "pscore", help="拾取会话打分：箱子/背包整体价值量化（L0 跳变 + L1 音频/红光晕 + L2 帧审）")
    sp.add_argument("targets", nargs="+",
                    help="场次目录 / 拾取片段目录（含 pickups.json），可多个")
    sp.add_argument("--frames", action="store_true",
                    help="同时抽取每段关键帧到 _frames/（供 L2 评审）")
    sp.add_argument("--skip-l1", action="store_true",
                    help="复用 l1/ 缓存，跳过音频/红光晕重新提取")
    sp.add_argument("--html", action="store_true",
                    help="（重新）生成 review.html，汇总全部已有 scores.json 场次")
    sp.set_defaults(func=cmd_pscore)

    sp = sub.add_parser("report", help="逐局战绩报告（事件池汇总，含多击倒20s）")
    sp.add_argument("video_id", nargs="?", default=None,
                    help="（保留位）当前报告直接汇总整个事件池")
    sp.set_defaults(func=cmd_report)

    sp = sub.add_parser(
        "pipeline", help="【应用端】全链：下载->ASR->说话人->OCR->三线切片->push 评估包")
    sp.add_argument("target", help="BV 号/URL（从下载开始）或本地场次目录/视频文件")
    sp.add_argument("--skip", nargs="*", default=[],
                    help="跳过的阶段：download asr speaker detect knockdowns "
                         "pickups voice push")
    sp.add_argument("--run-eval", action="store_true",
                    help="push 后触发服务端评估链（打分+报告+review 页）")
    sp.set_defaults(func=cmd_pipeline)

    sp = sub.add_parser(
        "pet", help="【应用端】桌面宠物客户端：置顶小窗显示识别链进展，"
                    "配置视频地址一键跑 pipeline")
    sp.add_argument("--address", default="",
                    help="预填视频地址（BV 号/URL/场次目录）")
    sp.set_defaults(func=cmd_pet)

    return p


def main(argv=None):
    args = build_parser().parse_args(argv)
    ensure_dirs()
    try:
        args.func(args)
    except NotImplementedError as e:
        print(f"\n[骨架未实现] {e}", file=sys.stderr)
        sys.exit(2)
    except ModuleNotFoundError as e:
        # 裁剪安装（端侧最小包不含 eval/score/pscore 等）下的友好提示
        print(f"\n[命令不可用] 本安装未含模块 {e.name}——"
              "该命令属服务端/完整版功能（打分/评估/混剪）。"
              "端侧最小包只支持识别链：download→asr→speaker→detect→"
              "knockdowns→pickups→voice→pipeline→push/pull。",
              file=sys.stderr)
        sys.exit(2)
    except (FileNotFoundError, RuntimeError, ValueError) as e:
        print(f"\n[错误] {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
