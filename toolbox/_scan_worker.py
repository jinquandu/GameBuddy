"""私有入口：并行扫描 worker，由 detector.scan_video 以独立子进程启动。

用法: python3 -m toolbox._scan_worker <video> <t_lo> <t_hi> <fps> <pack_json> <out_json>
"""
import json
import os
import sys

# 必须在 import cv2/onnxruntime 之前：多 worker 时每进程限 2 线程，
# 4 进程 × 2 = 8 核干净分摊（默认每进程请求全部核会互相拖慢）
os.environ.setdefault("OMP_NUM_THREADS", "2")

if __name__ == "__main__":
    from toolbox.detector import _scan_range

    video = sys.argv[1]
    t_lo, t_hi, fps = float(sys.argv[2]), float(sys.argv[3]), float(sys.argv[4])
    pack = tuple(json.loads(sys.argv[5]))
    out = sys.argv[6]
    raw = _scan_range(video, t_lo, t_hi, fps, pack)
    with open(out, "w", encoding="utf-8") as f:
        json.dump(raw, f, ensure_ascii=False)
