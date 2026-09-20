"""B 站分P视频下载（应用端 · 流程图节点 1）。

通用化原 download_bili.py：按 BV 号/_url 自动取分 P 清单，
1080P(qn=80) DASH avc1 + 最高音质，纯 python 续传（本机 curl 长传输会被
安全软件杀掉），ffmpeg 合并 mp4。产出约定 video/<标题>/<标题> <分P名>.mp4，
与 knockdown/pickups 的场次目录（P1..PN）识别兼容——分 P 名里带 "P<k>" 时
knockdown 会按此排序、跨分片续算。

用法：python3 -m toolbox download <BV号或视频页URL> [--parts 1,2,3] [--out 目录]
"""
import json
import os
import re
import subprocess
import time
import urllib.request
from pathlib import Path

from toolbox.config import ROOT, load_config

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")


def _cookie_str():
    jar = Path(load_config()["download"]["cookies_file"])
    if not jar.is_absolute():
        jar = ROOT / jar
    if not jar.is_file():
        raise RuntimeError(f"cookie 文件不存在：{jar}（从浏览器导出 Netscape 格式）")
    tab = "\t"
    parts = []
    for c in jar.read_text(encoding="utf-8").splitlines():
        if c and not c.startswith("#") and tab in c:
            cols = c.split(tab)
            if len(cols) >= 7:
                parts.append(f"{cols[5]}={cols[6]}")
    return "; ".join(parts)


def _hdrs():
    return {"User-Agent": UA, "Referer": "https://www.bilibili.com/",
            "Cookie": _cookie_str()}


def _http_json(url):
    req = urllib.request.Request(url, headers=_hdrs())
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.loads(r.read())


def parse_bvid(text):
    m = re.search(r"(BV[0-9A-Za-z]{10})", text)
    if not m:
        raise ValueError(f"未能从输入解析 BV 号：{text}")
    return m.group(1)


def fetch(url, dest: Path):
    """续传下载：Range 断点续传 + 最多 4 次重试。"""
    for attempt in range(4):
        try:
            pos = os.path.getsize(dest) if dest.exists() else 0
            h = dict(_hdrs())
            if pos:
                h["Range"] = f"bytes={pos}-"
            req = urllib.request.Request(url, headers=h)
            with urllib.request.urlopen(req, timeout=30) as r, open(dest, "ab") as f:
                total = int(r.headers.get("Content-Length", 0)) + pos
                n, last = pos, time.time()
                while True:
                    chunk = r.read(1 << 20)
                    if not chunk:
                        break
                    f.write(chunk)
                    n += len(chunk)
                    if time.time() - last > 20:
                        print(f"    {n/1e6:.0f}/{total/1e6:.0f}MB", flush=True)
                        last = time.time()
                if n == total:
                    return
                print(f"  连接中断于 {n/1e6:.0f}MB，续传...", flush=True)
        except Exception as e:
            print(f"  第{attempt+1}次出错: {type(e).__name__}: {str(e)[:100]}", flush=True)
            time.sleep(2)
    raise RuntimeError(f"下载失败: {url[:80]}")


def download(url_or_bvid, parts="all", out_dir=None):
    bvid = parse_bvid(url_or_bvid)
    view = _http_json(f"https://api.bilibili.com/x/web-interface/view?bvid={bvid}")
    if view.get("code") != 0:
        raise RuntimeError(f"取视频信息失败: {view.get('message')}")
    title = re.sub(r"[^\w\u4e00-\u9fff（）()]+", " ", view["data"]["title"]).strip()
    pages = view["data"]["pages"]            # [{cid, page, part, duration}]
    qn = int(load_config()["download"].get("qn", 80))

    want = None
    if parts != "all":
        want = set()
        for piece in re.split(r"[，,，\s]+", parts):
            if "-" in piece:
                a, b = piece.split("-", 1)
                want.update(range(int(a), int(b) + 1))
            elif piece:
                want.add(int(piece))

    out_root = Path(out_dir).expanduser() if out_dir else ROOT / "video" / title
    out_root.mkdir(parents=True, exist_ok=True)
    print(f"{title}  共 {len(pages)} P  ->  {out_root}")

    todo = [p for p in pages if want is None or p["page"] in want]
    if want is not None and not todo:
        raise ValueError(f"--parts {parts} 没有命中任何分 P（可用 1-{len(pages)}）")
    for p in todo:
        part = f"P{p['page']} {p['part']}"
        out = out_root / f"{title} {part}.mp4"
        print(f"[{todo.index(p)+1}/{len(todo)}] {part}", flush=True)
        if out.exists() and out.stat().st_size > 1_000_000:
            print("  已存在，跳过", flush=True)
            continue
        data = _http_json(f"https://api.bilibili.com/x/player/playurl?bvid={bvid}"
                          f"&cid={p['cid']}&qn={qn}&fnval=16&fourk=1")
        d = data.get("data") or {}
        if not d.get("dash"):
            raise RuntimeError(f"{part} 取播放流失败（cookie 过期或非大会员清晰度受限）: "
                               f"{data.get('message')}")
        vids = [v for v in d["dash"]["video"] if v["id"] == qn and v["codecs"].startswith("avc1")] \
            or [v for v in d["dash"]["video"] if v["id"] == qn] or d["dash"]["video"][:1]
        audio = max(d["dash"]["audio"], key=lambda a: a["bandwidth"])
        v = vids[0]
        print(f"  视频 {v['width']}x{v['height']} {v['codecs']} | "
              f"音频 {audio['codecs']} {audio['bandwidth']//1000}kbps", flush=True)
        fv, fa = Path(f"/tmp/bili_v_{p['cid']}.m4s"), Path(f"/tmp/bili_a_{p['cid']}.m4s")
        for tmp in (fv, fa):
            if tmp.exists():
                tmp.unlink()
        fetch(v["baseUrl"], fv)
        print(f"  视频完成 {fv.stat().st_size/1e6:.0f}MB", flush=True)
        fetch(audio["baseUrl"], fa)
        print(f"  音频完成 {fa.stat().st_size/1e6:.0f}MB", flush=True)
        r = subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", str(fv), "-i", str(fa),
                            "-c", "copy", "-movflags", "+faststart", str(out)])
        fv.unlink(missing_ok=True)
        fa.unlink(missing_ok=True)
        if r.returncode != 0:
            raise RuntimeError(f"ffmpeg 合并失败: {out}")
        print(f"  合并完成: {out.name} ({out.stat().st_size/1e6:.0f}MB)", flush=True)
    print(f"ALL_DONE -> {out_root}")
    return out_root