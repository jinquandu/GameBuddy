"""detector 拾取「连续确认制」回归：不依赖 OCR/ffmpeg，假 VideoCapture +
假 OCR 注入容器计数字符序列（同 queues_edge_test 的注入思路）。

背景（P0-1，2026-10）：计数增加分支曾无条件覆盖 pendings[name]，连续拾取
跳变 5->6->7 会把 5->6 静默丢掉；修法 = 连续再增先 flush 旧 pending
（与 pickup.py 会话内跳变同口径）。

跑法：py -3 tests/detector_loot_pending_regress.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import cv2          # noqa: E402
import numpy as np  # noqa: E402

import toolbox.detector as D  # noqa: E402

H, W = 1080, 1920
BX0, BY0 = int(W * D.SCAN_BAND[0]), int(H * D.SCAN_BAND[1])   # 信息带左上
TEXTS = []           # 每采样帧注入的 OCR 文本（下标 = 采样序号）
FAIL = []


def check(name, cond, extra=""):
    print(("PASS " if cond else "FAIL ") + name + (f" {extra}" if extra else ""))
    if not cond:
        FAIL.append(name)


class FakeCap:
    """按脚本出的假采集器：grab 推进一帧，帧内 (BY0,BX0) 像素编码采样序号。"""

    def __init__(self, _path):
        self.ts = [i + 1.0 for i in range(len(TEXTS))]
        self.i = -1

    def isOpened(self):
        return True

    def grab(self):
        self.i += 1
        return self.i < len(self.ts)

    def get(self, _prop):
        return self.ts[self.i] * 1000.0

    def retrieve(self):
        frame = np.zeros((H, W, 3), np.uint8)
        frame[BY0, BX0] = [self.i, self.i, self.i]
        return True, frame


class Cv2Shim:
    CAP_PROP_POS_MSEC = cv2.CAP_PROP_POS_MSEC
    VideoCapture = FakeCap


BOX = [(700, 500), (900, 500), (900, 540), (700, 540)]


def fake_ocr(img, use_cls=False):
    idx = int(img[0, 0, 0])
    texts = TEXTS[idx]
    if isinstance(texts, str):
        texts = [texts]
    return ([(BOX, tx, 0.9) for tx in texts], 0.0)


# 注入：假采集器/假 OCR/关批量引擎（不用真 CUDA 引擎）
D.cv2 = Cv2Shim
D._get_ocr = lambda: fake_ocr
D._get_batch_ocr = lambda: None


def loot_events(texts):
    TEXTS[:] = texts
    pack = ([], False, None, {})          # dance_kw/quality_on/qgrid/price_table
    raw = D._scan_range("fake.mp4", 0.0, 999.0, 1.0, pack)
    return [(r[2], r[0]) for r in raw if r[1] == "loot"]


# 连续再增（P0-1 主回归）：5->6->7 两条都要在，5->6 不再被覆盖丢掉
ev = loot_events(["背包：5/20", "背包：6/20", "背包：7/20", "背包：7/20"])
check("连续再增两条都记", ev == [("背包 5->6", 2.0), ("背包 6->7", 3.0)], f"got {ev}")

# 常规确认：增加后下一帧保持才记（时间取增加帧，非确认帧）
ev = loot_events(["背包：5/20", "背包：6/20", "背包：6/20", "背包：6/20"])
check("常规确认一条", ev == [("背包 5->6", 2.0)], f"got {ev}")

# 计数回落 = 视角抖动，丢弃
ev = loot_events(["背包：5/20", "背包：6/20", "背包：5/20", "背包：5/20"])
check("回落丢弃", ev == [], f"got {ev}")

# 增加后即到片尾：pending 未获确认帧，不记（维持原语义）
ev = loot_events(["背包：5/20", "背包：5/20", "背包：6/20"])
check("片尾未确认不记", ev == [], f"got {ev}")

# 多容器互不干扰：安全箱常规确认、背包连续再增（第 5 帧一帧双文本）
ev = loot_events(["背包：5/20", "安全箱：1/8", "背包：6/20", "安全箱：2/8",
                  ["背包：7/20", "安全箱：2/8"]])
ev_set = set(ev)
check("多容器混合", ev_set == {("背包 5->6", 3.0), ("安全箱 1->2", 4.0)},
      f"got {ev}")

print("\n" + ("全部通过 ✅" if not FAIL else f"失败 {len(FAIL)} 项 ❌：{FAIL}"))
sys.exit(1 if FAIL else 0)
