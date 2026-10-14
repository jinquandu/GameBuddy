"""queues 回溯定位逻辑单测：不依赖 OCR/ffmpeg，注入假 ROI 文本序列。

跑法：py -3 tests/queues_edge_test.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from toolbox.queues import _find_queue_moment  # noqa: E402


def fake(texts):
    return lambda src, t: texts.get(t, "")


def case(name, texts, tm, floor, want):
    got = _find_queue_moment("fake.mp4", tm, floor, fake(texts))
    got_n = (got[0], got[1]) if got else None
    assert got_n == want, f"{name}: want {want}, got {got_n}"
    print(f"  ok {name}: {got_n}")


# 组队：匹配中持续多帧，倒序首个「匹配」命中 = 距 match_start 最近的一帧
case("team-匹配中",
     {**{t: "" for t in range(80, 119)},
      **{t: "匹配中 00:22" for t in range(40, 80)}},
     tm=119, floor=0, want=(79, "匹配中"))

# 单排：无匹配中态，点击前最后一张带「出发」按钮的帧
case("solo-出发",
     {**{t: "" for t in range(477, 505)},
      **{t: "配装 出发" for t in range(300, 477)}},
     tm=505, floor=205, want=(476, "出发"))

# 窗口下限生效：按钮帧全在 floor 之前，不越界回溯
case("floor-限界",
     {t: "出发" for t in range(0, 100)},
     tm=505, floor=205, want=None)

# 全程无按钮（如点击发生在视频开始前）
case("none", {}, tm=100, floor=0, want=None)

# 展示画面头像栏干扰文本不应命中
case("noise-不误命中",
     {t: ("Knight傲 内水里OvO 美女拉结" if t >= 84 else "") for t in range(0, 119)},
     tm=119, floor=0, want=None)

print("queues_edge_test: all passed")
