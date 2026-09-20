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


def probe_video(path):
    """ffprobe 读取时长与分辨率，返回 dict；失败抛 RuntimeError。"""
    cmd = [
        "ffprobe", "-v", "error", "-print_format", "json",
        "-show_format", "-show_streams", str(path),
    ]
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(f"ffprobe 失败: {path}\n{r.stderr.strip()[:300]}")
    info = json.loads(r.stdout)
    video = next((s for s in info.get("streams", [])
                  if s.get("codec_type") == "video"), {})
    fmt = info.get("format", {})
    return {
        "duration_sec": round(float(fmt.get("duration", 0)), 1),
        "width": video.get("width"),
        "height": video.get("height"),
    }


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
