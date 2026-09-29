"""私有入口：批量局外判别 worker，由 pickup.lobby_checks_parallel 启动。

一次进程顺序判别一批会话窗口（整帧 OCR 每帧 1-2s，串行一场 ~27 会话要
5-8min）。逐会话 append 一行 JSON 并 flush，主进程轮询增量写缓存。

用法: python -m toolbox._lobby_batch_worker <video> <jobs_json> <out_jsonl>
jobs_json: [{"key","t0","t1"}, ...]
每行输出: {"key","lobby":bool}
"""
import json
import os
import sys

os.environ.setdefault("OMP_NUM_THREADS", "2")
os.environ.setdefault("TOOLBOX_OCR_THREADS", "2")

if __name__ == "__main__":
    import cv2
    cv2.setNumThreads(2)
    from toolbox.pickup import _lobby_check

    video = sys.argv[1]
    jobs = json.loads(open(sys.argv[2], encoding="utf-8").read())
    out_file = sys.argv[3]
    with open(out_file, "w", encoding="utf-8", newline="\n") as f:
        for j in jobs:
            try:
                lobby = _lobby_check(video, j["t0"], j["t1"])
            except Exception:
                lobby = False          # 判别失败按对局保留（宁可多留人工剔）
            f.write(json.dumps({"key": j["key"], "lobby": lobby},
                               ensure_ascii=False) + "\n")
            f.flush()
