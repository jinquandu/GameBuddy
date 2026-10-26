"""分发包打包(REQ-20261022-009):标准正斜杠 ZIP + 用户说明 + 发布清单。

为什么自己写而不是 Compress-Archive:PowerShell 5.1 的 Compress-Archive 在
Windows 上写入的条目分隔符是反斜杠(历史 r1~r6 全部如此),而 ZIP 规范要求
正斜杠;跨平台解压工具会把 `Dir\\file` 当成一个文件名,产生异常目录。本脚本
用 zipfile 逐项写入,强制 `/`,并校验无绝对路径/无 `..`/无重复项。

用法:
  py -3 tools/make_release.py --src dist/GameBuddy --out dist/GameBuddy-core.zip
产出额外写 dist/release-manifest.txt(条目数/大小/SHA256/分隔符校验)。
"""
import argparse
import hashlib
import sys
import zipfile
from pathlib import Path

DOC_NAME = "快速开始.md"
DOC = """# 陪玩大师姐 GameBuddy · 快速开始

## 安装（便携版，无需安装程序）

1. 把下载的 ZIP **完整解压**到一个有写权限的目录（例如 `D:\\GameBuddy`）。
   - 不要直接在压缩包里双击运行，也不要只解压一部分。
2. 双击运行目录里的 `GameBuddy.exe`。
3. 首次运行会在同一目录生成 `data\\`，账号、设备、队列与日志都在里面。

## 第一次使用

1. 账号设置 → 登录（手机号 + 短信验证码）。
2. 工作台 → 更换文件夹，选一个存放游戏录像的文件夹。
3. 账号设置 → 开始剪辑的条件，确认「登录 / 权益 / 微信 / 环境」四项。
4. 之后保持程序运行即可，它会自动检查新增录像并处理。

## 升级

- 用新版 ZIP 完整解压到新的目录，再把旧目录的 `data\\` **整个复制**到新目录。
- `data\\` 里是账号登录状态与设备身份：删掉会需要重新登录并重新验证设备；
  不要把它发给别人。
- 升级不需要卸载旧版；确认新版可用后再删旧目录。

## 卸载

- 直接删除整个程序目录即可，不会在系统里残留服务或注册表项。
- 如需保留使用记录，先备份 `data\\`。

## 排障

- 账号设置 → 复制诊断信息，把内容发给大师姐（不含手机号与令牌）。
- 日志在 `data\\logs\\`，其中 `pet.log` 是主流程、`pipeline.log` 是剪辑流程。

## 体积说明

约 388MB 主要来自：视频工具 ffmpeg/ffprobe、OCR 模型（onnx）与运行时库。
这些是本地识别与剪辑的必要依赖，不能裁剪。

## 版本

程序版本恒为 V0.1（界面左下角与「账号设置」页可见）。遇到问题点
「账号设置 → 复制诊断信息」，里面的构建号能帮大师姐定位你用的具体版本。
"""


def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for blob in iter(lambda: f.read(1 << 20), b""):
            h.update(blob)
    return h.hexdigest()


def build(src, out, extra_docs=True):
    src = Path(src).resolve()
    out = Path(out).resolve()
    if not src.is_dir():
        raise SystemExit(f"源目录不存在: {src}")
    if extra_docs:
        doc = src / DOC_NAME
        if not doc.exists():
            doc.write_text(DOC, encoding="utf-8")
    # 顶层保留源目录名(解压得到 GameBuddy/... 而不是散落当前目录)
    top = src.name
    entries = [(src, top + "/")]
    for p in sorted(src.rglob("*")):
        rel = top + "/" + p.relative_to(src).as_posix()
        entries.append((p, rel + "/" if p.is_dir() else rel))
    names = [a for _, a in entries]
    assert len(names) == len(set(names)), "重复条目"
    for a in names:
        assert not a.startswith("/") and "\\" not in a, f"非法条目:{a}"
        assert ".." not in a.split("/"), f"上级跳转:{a}"
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_suffix(out.suffix + ".tmp")
    with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as z:
        for p, arc in entries:
            if p.is_dir():
                z.writestr(zipfile.ZipInfo(arc), b"")
            else:
                z.write(p, arc)
    tmp.replace(out)
    z = zipfile.ZipFile(out)
    bad = z.testzip()
    if bad:
        raise SystemExit(f"ZIP 校验失败:{bad}")
    datei = out.stat().st_size
    digest = sha256(out)
    manifest = [
        f"file={out.name}",
        f"entries={len(names)}",
        f"size={datei}",
        f"sha256={digest}",
        "separator=forward-slash (all entries)",
        "abs_or_dotdot=none",
    ]
    man = out.parent / "release-manifest.txt"
    man.write_text("\n".join(manifest) + "\n", encoding="utf-8")
    return {"file": str(out), "entries": len(names), "size": datei,
            "sha256": digest, "manifest": str(man)}


def main(argv):
    ap = argparse.ArgumentParser()
    ap.add_argument("--src", required=True, help="待打包目录(如 dist/GameBuddy)")
    ap.add_argument("--out", required=True, help="输出 ZIP 路径")
    ap.add_argument("--no-doc", action="store_true", help="不写快速开始.md")
    a = ap.parse_args(argv)
    info = build(a.src, a.out, extra_docs=not a.no_doc)
    for k, v in info.items():
        print(f"{k}: {v}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
