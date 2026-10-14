"""点击匹配时刻截图：对每局（match_start）回溯定位「点击匹配」并抽帧。

定位口径（REQ-20261014-001，2026-10-14 两来源时间线实测）：
- 从 match_start 前 1 秒起**倒序** 1fps 采样，只 OCR 大厅右下按钮区（ROI）；
- 命中「匹配*」（组队：按钮态「匹配中」，离 match_start 最近的命中帧即
  点击后等待态）或「出发」（单排：按钮态「出发 | 配装」，倒序首个命中帧
  即点击前一瞬）即停——倒序首个命中 = 距入局最近的大厅按钮帧；
- 组队匹配等待期可 >60s 且点击时刻离 match_start 不恒定（单排 ~28s、
  组队 ≥60s），故不能拿 match_start 减固定偏移，必须逐帧回溯。

事件流只读复用（existing_events，绝不触发整段重扫）：无事件流的分片跳过。
回溯窗口下限：上一局 match_start+60s（其时上一局必已在对局内，大厅按钮
不可见，不会误命中）与 300s 硬上限取大者。

产出：data/queues/<场次>/NN_P<k>_<mmss>_<触发词>.jpg + queues.json 索引。
幂等：索引按 P 号+match 秒记键，已有命中或已记跳过的局不再重扫（--force 重来）。
"""
import json
import os
import subprocess
from pathlib import Path

import cv2
import numpy as np

from toolbox import proc
from toolbox.config import DATA, load_config
from toolbox.events import collect_videos, existing_events, part_no
from toolbox.naming import mmss as _mmss, slug as _slug

# 按钮 ROI（相对全帧）：实测两来源「匹配中 / 出发 | 配装」按钮均落在
# x>=76%, y 87-94%；放宽到 x>=60%, y>=74% 容纳窗口化/布局差异。
# 世界聊天在左下、击杀播报在顶部，均不入 ROI，天然免误命中。
_ROI = (0.60, 0.74, 1.0, 1.0)
_WINDOW_SEC = 300          # 单局回溯硬上限
_PREV_GUARD = 60           # 距上一局 match_start 的保护间隔
_MAX_FRAME_FAILS = 5       # 连续取帧失败（片尾/坏块）即放弃该局


def _grab_frame(src: Path, t: float):
    """取 t 秒整帧（BGR ndarray）；失败重试一次，仍败返回 None。"""
    for _ in range(2):
        r = subprocess.run(
            ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
             "-ss", f"{max(0.0, t):.2f}", "-i", str(src),
             "-frames:v", "1", "-f", "image2pipe", "-c:v", "bmp", "pipe:1"],
            capture_output=True, creationflags=proc.NO_WINDOW)
        if r.returncode == 0 and r.stdout:
            return cv2.imdecode(np.frombuffer(r.stdout, np.uint8),
                                cv2.IMREAD_COLOR)
    return None


def _roi_text(img, ocr) -> str:
    """按钮区 OCR 文本拼接（RapidOCR 惯用：res -> [box, text, score]）。"""
    h, w = img.shape[:2]
    x0, y0, x1, y1 = _ROI
    crop = img[int(h * y0):int(h * y1), int(w * x0):int(w * x1)]
    res, _ = ocr(crop, use_cls=False)
    return "".join((t[1] or "") for t in (res or []))


def _find_queue_moment(src, tm, floor, roi_text):
    """倒序找距 match_start 最近的按钮帧，返回 (t, 触发词) 或 None。

    roi_text(src, t) -> str 由调用方注入（真实=ffmpeg+OCR，测试=假字典），
    本函数只含可单测的回溯/判定逻辑。
    """
    for t in range(int(tm) - 1, int(floor) - 1, -1):
        txt = roi_text(src, t) or ""
        if "匹配" in txt:
            return t, "匹配中", txt
        if "出发" in txt:
            return t, "出发", txt
    return None


def queues(target, config=None, force=False):
    """链路入口：每局定位点击匹配时刻并抽帧，返回产出目录。"""
    config = config or load_config()
    videos = collect_videos(target)
    session = (videos[0].parent.name if len(videos) > 1 or Path(target).is_dir()
               else videos[0].stem)
    out_dir = DATA / "queues" / _slug(session, 60)
    index_path = out_dir / "queues.json"
    index = (json.loads(index_path.read_text(encoding="utf-8"))
             if index_path.is_file() else {"session": session, "shots": []})
    by_key = {s.get("key"): s for s in index["shots"]}

    ocr = None
    seq = len(index["shots"])
    n_new = 0
    for video in videos:
        found = existing_events(video)
        if not found:
            print(f"  {video.name[:36]}：无已有事件流，跳过（先跑 detect）")
            continue
        _, events = found
        starts = sorted(e["t_start"] for e in events
                        if e.get("kind") == "match_start")
        if not starts:
            print(f"  {video.name[:36]}：无 match_start 事件，跳过")
            continue
        part = part_no(video)
        print(f"  {video.name[:36]}：{len(starts)} 局")
        fails = 0
        for i, tm in enumerate(starts):
            key = f"P{part}#{int(tm)}"
            prev = index["shots"] and by_key.get(key)
            if prev and not force and (prev.get("file") or prev.get("skip")):
                continue            # 已命中或已记跳过，幂等复用
            floor = 0 if i == 0 else max(
                int(starts[i - 1]) + _PREV_GUARD, int(tm) - _WINDOW_SEC)
            floor = min(floor, int(tm) - 1)

            # 逐秒回溯：真实取帧+OCR（cache 免重复采样；取帧连败即放弃）
            hit = None
            n_ocr = 0
            cache = {}
            hit_img = None
            for t in range(int(tm) - 1, int(floor) - 1, -1):
                if t not in cache:
                    img = _grab_frame(video, t)
                    if img is None:
                        fails += 1
                        cache[t] = ""
                        if fails >= _MAX_FRAME_FAILS:
                            break
                        continue
                    fails = 0
                    if ocr is None:
                        from toolbox.detector import _get_ocr
                        ocr = _get_ocr()
                    cache[t] = _roi_text(img, ocr)
                txt = cache[t] or ""
                n_ocr += 1
                if "匹配" in txt or "出发" in txt:
                    trigger = "匹配中" if "匹配" in txt else "出发"
                    hit = (t, trigger, txt)
                    hit_img = img          # 命中帧已在手上，落盘免二次取帧
                    break
            if hit is None:
                reason = ("连续取帧失败" if fails >= _MAX_FRAME_FAILS
                          else f"窗口内未见匹配按钮（回溯 {n_ocr} 帧）")
                seq += 1
                index["shots"].append({"key": key, "part": part,
                                       "t_match_start": tm, "skip": reason})
                by_key[key] = index["shots"][-1]
                print(f"    [{seq:02d}] P{part} {_mmss(tm)} 局：未定位（{reason}）")
                continue
            t_q, trigger, txt = hit
            seq += 1
            out = out_dir / f"{seq:02d}_P{part}_{_mmss(t_q)}_{trigger}.jpg"
            out_dir.mkdir(parents=True, exist_ok=True)
            if hit_img is None:               # 理论不可达，兜底再取一次
                hit_img = _grab_frame(video, t_q)
            if hit_img is None:
                raise RuntimeError(f"抽帧失败：{video.name} @{t_q}s")
            # cv2.imwrite 在 Windows 上不支持中文路径，走 imencode+tofile
            ok, buf = cv2.imencode(".jpg", hit_img,
                                   [cv2.IMWRITE_JPEG_QUALITY, 95])
            if not ok:
                raise RuntimeError(f"JPEG 编码失败：{video.name} @{t_q}s")
            buf.tofile(str(out))
            index["shots"].append({
                "key": key, "file": out.name, "part": part,
                "t_match_start": tm, "t_queue": t_q, "trigger": trigger,
                "roi_text": txt[:60], "src": str(video)})
            by_key[key] = index["shots"][-1]
            n_new += 1
            print(f"    [{seq:02d}] P{part} {_mmss(t_q)} {trigger} "
                  f"(match@{_mmss(tm)})")

    out_dir.mkdir(parents=True, exist_ok=True)
    tmp = index_path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(index, ensure_ascii=False, indent=1),
                   encoding="utf-8")
    os.replace(tmp, index_path)
    print(f"共 {len([s for s in index['shots'] if s.get('file')])} 张匹配截图 "
          f"（本次新增 {n_new}）+ queues.json 已写入 {out_dir}")
    return out_dir
