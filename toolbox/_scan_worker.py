"""私有入口：并行扫描 worker，由 detector.scan_video 以独立子进程启动。

用法: python3 -m toolbox._scan_worker <video> <t_lo> <t_hi> <fps> <pack_json> <out_json> [emit_from]
emit_from：预热边界，t<emit_from 的事件丢弃（跨段状态重建，见 scan_video）。
"""
import json
import os
import sys

# 必须在 import cv2/onnxruntime 之前：多 worker 时每进程限 2 线程，
# N worker × 2 = 核数干净分摊（默认每进程请求全部核会互相拖慢）。
# 注意 OMP_NUM_THREADS 对 onnxruntime 线程池无效——ORT 侧由
# TOOLBOX_OCR_THREADS 生效（见 detector._get_ocr）；cv2 解码线程池同因
# 默认吃满全核，11 进程 × 全核解码线程实测互相拖慢 ~3x，这里一并限住。
os.environ.setdefault("OMP_NUM_THREADS", "2")
os.environ.setdefault("TOOLBOX_OCR_THREADS", "2")

if __name__ == "__main__":
    import cv2
    cv2.setNumThreads(2)
    from toolbox.detector import _scan_range

    video = sys.argv[1]
    t_lo, t_hi, fps = float(sys.argv[2]), float(sys.argv[3]), float(sys.argv[4])
    pack = tuple(json.loads(sys.argv[5]))
    out = sys.argv[6]
    emit_from = float(sys.argv[7]) if len(sys.argv) > 7 else 0.0
    raw = _scan_range(video, t_lo, t_hi, fps, pack, emit_from=emit_from)
    with open(out, "w", encoding="utf-8") as f:
        json.dump(raw, f, ensure_ascii=False)
