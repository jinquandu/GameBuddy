"""视频导入登记：把（目前为下载的）视频复制进 data/recordings/ 并登记元数据。

登记表 index.json 是全流程的唯一入口，后续模块以 video_id 为键定位素材。
录屏采集器（OBS 控制等）接入时，产出写入同一目录并调用 register() 即可。
"""
import json
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path

from toolbox.config import INDEX_FILE, RECORDINGS, ensure_dirs


def probe_video(path, attempts=2):
    """ffprobe 读取时长与分辨率，返回 dict；失败抛 RuntimeError。

    空输出/非零退出/坏 JSON 均重试一次再报错（2026-10-28 客户机曾遇
    ffprobe stdout 未被捕获，json.loads(None) 裸 TypeError 打断环境检测
    第 6 步——疑似安全软件干扰子进程管道，重试 + 可读报错兜底）。"""
    from toolbox import proc
    cmd = [
        "ffprobe", "-v", "error", "-print_format", "json",
        "-show_format", "-show_streams", str(path),
    ]
    err = ""
    for _ in range(max(1, attempts)):
        r = subprocess.run(cmd, capture_output=True, text=True,
                           creationflags=proc.NO_WINDOW)
        out, errtxt = (r.stdout or "").strip(), (r.stderr or "").strip()
        if r.returncode != 0:
            err = f"退出码 {r.returncode}：{errtxt[:300] or '无输出'}"
            continue
        if not out:
            err = "无标准输出（若反复出现，请将本程序加入安全软件白名单后重试）"
            continue
        try:
            info = json.loads(out)
        except ValueError as e:
            err = f"输出不是有效 JSON：{str(e)[:120]}"
            continue
        video = next((s for s in info.get("streams", [])
                      if s.get("codec_type") == "video"), {})
        fmt = info.get("format", {})
        return {
            "duration_sec": round(float(fmt.get("duration", 0)), 1),
            "width": video.get("width"),
            "height": video.get("height"),
        }
    raise RuntimeError(f"ffprobe 失败: {path}\n{err}")


def load_index():
    if INDEX_FILE.exists():
        return json.loads(INDEX_FILE.read_text(encoding="utf-8"))
    return []


def save_index(rows):
    INDEX_FILE.write_text(
        json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")


def _next_id(rows):
    nums = [int(r["id"][1:]) for r in rows if r["id"].startswith("v")]
    return f"v{max(nums, default=0) + 1:03d}"


def ingest_file(path, game=None, source="download"):
    """导入单个视频：已登记（按源路径判重）则跳过，否则复制并登记。

    返回 (record, created)。
    """
    path = Path(path).expanduser().resolve()
    if not path.is_file():
        raise FileNotFoundError(f"视频不存在: {path}")
    ensure_dirs()

    rows = load_index()
    for r in rows:
        if r["source_path"] == str(path):
            print(f"已登记过，跳过: {r['id']} -> {r['file']}")
            return r, False

    meta = probe_video(path)
    video_id = _next_id(rows)
    dst = RECORDINGS / f"{video_id}_{path.name}"
    rec = {
        "id": video_id,
        "file": dst.name,
        "source_path": str(path),
        "source": source,             # download / screen_record（后续）
        "game": game,
        "imported_at": datetime.now().isoformat(timespec="seconds"),
        **meta,
    }
    shutil.copy2(path, dst)
    rows.append(rec)
    save_index(rows)
    print(f"已导入 {rec['id']}: {dst.name} "
          f"({rec['duration_sec']}s, {rec['width']}x{rec['height']})")
    return rec, True


def recording_path(video_id):
    """按 video_id 找到已导入的视频文件路径，找不到抛 FileNotFoundError。"""
    for r in load_index():
        if r["id"] == video_id:
            p = RECORDINGS / r["file"]
            if not p.is_file():
                raise FileNotFoundError(f"登记表有 {video_id} 但文件缺失: {p}")
            return p
    raise FileNotFoundError(f"未登记的视频 id: {video_id}（先运行 ingest）")


def list_recordings():
    return load_index()
