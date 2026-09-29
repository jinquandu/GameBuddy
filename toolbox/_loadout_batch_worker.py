"""私有入口：loadout 补扫并行 worker，由 detector._scan_loadouts_parallel 启动。

用法: python -m toolbox._loadout_batch_worker <video> <t_match> <tables_json> <out_json>
tables_json: {"operator_class": {...}, "weapon_type": {...}}（config 合并后的全表）
"""
import json
import os
import sys

# 必须在 import onnxruntime 之前：并行 worker 每进程限 2 OCR 线程
# （OMP_NUM_THREADS 对 ORT 线程池无效，见 detector._get_ocr 注释）
os.environ.setdefault("OMP_NUM_THREADS", "2")
os.environ.setdefault("TOOLBOX_OCR_THREADS", "2")

if __name__ == "__main__":
    import cv2
    cv2.setNumThreads(2)
    from toolbox.detector import _scan_loadout

    video = sys.argv[1]
    t_match = float(sys.argv[2])
    tables = json.loads(sys.argv[3])
    out = sys.argv[4]
    meta = _scan_loadout(video, t_match,
                         tables["operator_class"], tables["weapon_type"])
    with open(out, "w", encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=False)
