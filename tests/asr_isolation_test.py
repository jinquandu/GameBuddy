"""asr() 单分P隔离回归（P0-2，2026-10）：无音轨/短分P等单分P失败只跳过
该分P，返回列表仍与分片按位对齐（失败位 None），全部分P失败才抛。

不加载真模型/ffmpeg：_load_model 与 transcribe_part 注入桩。

跑法：py -3 tests/asr_isolation_test.py
"""
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import toolbox.asr as A  # noqa: E402

FAIL = []


def check(name, cond, extra=""):
    print(("PASS " if cond else "FAIL ") + name + (f" {extra}" if extra else ""))
    if not cond:
        FAIL.append(name)


tmp = Path(tempfile.mkdtemp(prefix="asr_iso_"))
names = ["猪猪 P1 01日12点.mp4", "猪猪 P2 01日13点.mp4", "猪猪 P3 01日14点.mp4"]
for n in names:
    (tmp / n).write_bytes(b"")          # collect_videos 只认扩展名
A._load_model = lambda: "fake-model"


def stub_transcribe(video, model):
    video = Path(video)
    if "P2" in video.name:
        raise RuntimeError("无音轨")    # 模拟坏分P
    part = "P1" if "P1" in video.name else "P3"
    return tmp / f"asr_{part}", 3


A.transcribe_part = stub_transcribe

dirs = A.asr(str(tmp))
check("按位对齐失败位 None",
      [d is not None for d in dirs] == [True, False, True], f"got {dirs}")
check("好分P返回目录", dirs[0] == tmp / "asr_P1" and dirs[2] == tmp / "asr_P3")


def stub_all_fail(video, model):
    raise RuntimeError("无音轨")


A.transcribe_part = stub_all_fail
try:
    A.asr(str(tmp))
    check("全失败抛 RuntimeError", False, "未抛")
except RuntimeError as e:
    check("全失败抛 RuntimeError", "全部分片转写失败" in str(e), f"got {e!r}")

print("\n" + ("全部通过 ✅" if not FAIL else f"失败 {len(FAIL)} 项 ❌：{FAIL}"))
sys.exit(1 if FAIL else 0)
