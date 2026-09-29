"""私有入口：批量拾取小扫描 worker，由 pickup.scan_islands_parallel 启动。

一次进程顺序处理一批岛窗口（OCR 模型只加载一次；旧版每岛一个进程，一场
31 岛要付 31 次解释器+模型启动）。逐岛 append 一行 JSON 并 flush——主进程
轮询即可增量落盘缓存（断点续跑）。

用法: python -m toolbox._pickup_scan_batch_worker <video> <jobs_json> <out_jsonl>
jobs_json: [{"key","t_lo","t_hi","fps","it0","it1"}, ...]
每行输出: {"key","ok","sessions","it0","it1"}（ok=false 时带 err）
"""
import json
import os
import sys

# 并行 worker 每进程限 2 OCR 线程（OMP 对 ORT 线程池无效，见 detector._get_ocr）
os.environ.setdefault("OMP_NUM_THREADS", "2")
os.environ.setdefault("TOOLBOX_OCR_THREADS", "2")

if __name__ == "__main__":
    import cv2
    cv2.setNumThreads(2)
    from toolbox.pickup import scan_ui_sessions

    video = sys.argv[1]
    jobs = json.loads(open(sys.argv[2], encoding="utf-8").read())
    out_file = sys.argv[3]
    with open(out_file, "w", encoding="utf-8", newline="\n") as f:
        for j in jobs:
            row = {"key": j["key"], "ok": True,
                   "it0": j.get("it0", 0), "it1": j.get("it1", 0)}
            try:
                row["sessions"] = scan_ui_sessions(
                    video, j["t_lo"], j["t_hi"], fps=j.get("fps", 1.0))
            except Exception as ex:
                row.update(ok=False, err=str(ex))
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
            f.flush()
