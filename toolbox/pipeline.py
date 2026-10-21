"""应用端全链流水线（两步上传模型的客户端链路）。

download(B站) -> asr -> speaker -> detect(OCR事件池) -> knockdowns -> pickups
-> queues(点击匹配截图) -> voice -> push(评估包)。识别重活全在应用端 Apple
Silicon；服务端收评估包跑打分/打标（toolbox eval），被选中做片的场次再
`push --mode video` 补传源片跑混剪（highlight/remix），应用端 pull 拉回成片。

用法：
    python3 -m toolbox pipeline <BV号或URL>            # 从下载开始
    python3 -m toolbox pipeline video/<场次目录>        # 素材已在本地
    --skip download asr speaker detect knockdowns pickups queues voice push
"""
import re
import time
from pathlib import Path


def _bvish(target):
    return bool(re.search(r"(BV[0-9A-Za-z]{10}|bilibili\.com)", target))


def _voice_line_ready(config):
    """语音三阶段（asr/speaker/voice）可用性，返回 (ok, 原因)。

    features.asr 关闭，或 funasr/torch/torchaudio/sklearn/librosa 任一未安装
    （核心版只装 requirements.txt）时整线不可用。用 find_spec 探测而非真
    import：跑链每次探测不该白付 torch 的模块加载开销，也不触发缺包报错。
    """
    if not (config.get("features") or {}).get("asr", True):
        return False, "features.asr=false"
    import importlib.util
    missing = [m for m in ("funasr", "torch", "torchaudio", "sklearn", "librosa")
               if importlib.util.find_spec(m) is None]
    if missing:
        return False, "未装语音线依赖（" + "/".join(missing) + "）"
    return True, ""


def run_pipeline(target, config=None, skip=(), run_eval=True):
    """按阶段顺序跑，已有产物自动复用（各链路自身幂等）。返回各阶段耗时。

    run_eval 默认 True（REQ-20261021-002）：跑链末段 push 评估包默认触发
    服务端评估链——此前后台日任务链不透传该参数，上传后打标站 0 片段、
    均须服务端人工补跑 eval。显式关闭走 CLI --no-run-eval。"""
    from toolbox.account import require_login
    from toolbox.config import load_config
    config = config or load_config()
    require_login(config)               # 登录门控（REQ-20261019-013）
    skip = set(skip)
    spent = {}

    def stage(name, fn):
        if name in skip:
            print(f"[pipeline] -- 跳过 {name}")
            return None
        t = time.time()
        print(f"[pipeline] {name} 开始 ...", flush=True)
        out = fn()
        spent[name] = round(time.time() - t, 1)
        print(f"[pipeline] {name} 完成 ({spent[name]:.0f}s)", flush=True)
        return out

    if _bvish(target):
        if "download" in skip:
            raise RuntimeError("目标是 BV 链接但 download 被跳过：应传本地场次目录")
        from toolbox.download import download
        target = str(stage("download", lambda: download(target)))
    skip.add("download")

    from toolbox.events import collect_videos, events_for
    videos = collect_videos(target)
    session = Path(target).name
    print(f"[pipeline] 场次：{session}，{len(videos)} 个分片")

    # 语音线可用性：开关关或核心版缺依赖 -> asr/speaker/voice 整线自动跳过
    # （OCR 击倒/拾取/push 不受影响）；用户显式 --skip 了则不重复提示。
    vline, why = _voice_line_ready(config)
    if not vline and not ({"asr", "speaker", "voice"} & skip):
        skip |= {"asr", "speaker", "voice"}
        print(f"[pipeline] 语音线未启用（{why}）：跳过 asr/speaker/voice；"
              "pip install -r requirements-asr.txt 可启用（装好后 features.asr "
              "保持 true 即可）", flush=True)

    asr_dirs = {}

    def do_asr():
        nonlocal asr_dirs
        from toolbox.asr import asr
        # 失败分P为 None（单分P已隔离，见 asr()），speaker/voice 会跳过 None
        dirs = asr(target)                      # 已转写的分片自动跳过
        by_name = {Path(str(v)).name: d for v, d in zip(videos, dirs)}
        asr_dirs = by_name
        return list(d for d in dirs if d)

    stage("asr", do_asr)

    if "asr" in skip:
        # --skip asr 只该跳过转写本身：从已有产物重建 分片->asr目录 映射，
        # 否则下面的 speaker/voice 会遍历空映射静默空转（还照常打印「完成」）
        from toolbox.asr import find_transcript_dir
        asr_dirs = {v.name: find_transcript_dir(v) for v in videos}
        missing = [v for v, d in asr_dirs.items() if not d]   # 键已是分片名
        if missing:
            shown = "、".join(missing[:3]) + ("…" if len(missing) > 3 else "")
            print(f"[pipeline] 警告：{len(missing)}/{len(videos)} 个分片无已有"
                  f"转写产物（{shown}），speaker/voice 将跳过它们", flush=True)

    def do_speaker():
        from toolbox import speaker
        outs, skipped = [], []
        for d in asr_dirs.values():
            if not (d and (d / "transcript.json").is_file()):
                continue
            try:                   # 单分P失败只跳过它（如 wav 被清理）
                out, _ = speaker.run(d / "transcript.json")
                outs.append(out)
            except Exception as e:    # noqa: BLE001 分P相互独立，隔离故障
                skipped.append(f"{Path(d).name}({type(e).__name__})")
                print(f"[pipeline] speaker 跳过 {Path(d).name}："
                      f"{type(e).__name__}: {e}", flush=True)
        if skipped:
            print(f"[pipeline] speaker 部分完成（{len(skipped)} 个分P失败："
                  f"{', '.join(skipped[:6])}）", flush=True)
        return outs

    stage("speaker", do_speaker)

    def do_detect():
        """OCR 事件池：复用已有事件流，缺失的当场检测，落规范 <分片>_full。"""
        return [events_for(v, config) for v in videos]

    stage("detect", do_detect)

    def do_knockdowns():
        from toolbox.knockdown import knockdowns
        return knockdowns(target, config)

    def do_pickups():
        from toolbox.pickup import pickups
        return pickups(target, config)

    stage("knockdowns", do_knockdowns)
    stage("pickups", do_pickups)

    def do_queues():
        from toolbox.queues import queues
        return queues(target, config)

    stage("queues", do_queues)

    def do_voice():
        from toolbox.voice import voice_session
        outs, skipped = [], []
        for d in asr_dirs.values():
            if not (d and (d / "transcript.json").is_file()):
                continue
            try:                       # 单分P失败只跳过它（如无 host 语音）
                outs.append(voice_session(str(d), config))
            except Exception as e:    # noqa: BLE001 分P相互独立，隔离故障
                skipped.append(f"{Path(d).name}({type(e).__name__})")
                print(f"[pipeline] voice 跳过 {Path(d).name}："
                      f"{type(e).__name__}: {e}", flush=True)
        if skipped:
            print(f"[pipeline] voice 部分完成（{len(skipped)} 个分P无 host 语音："
                  f"{', '.join(skipped[:6])}）", flush=True)
        return outs

    stage("voice", do_voice)

    def do_push():
        from toolbox.transfer import push
        push(target, mode="eval", run_eval=run_eval)
        return "ok"

    stage("push", do_push)

    print("[pipeline] 全链完成。" + " ".join(f"{k}={v}s" for k, v in spent.items()))
    print("[pipeline] 下一步：评估人员打标；选中做片时："
          f"python3 -m toolbox push \"{target}\" --mode video，"
          "再在服务端跑 remix，应用端 pull 拉回成片。")
    return spent