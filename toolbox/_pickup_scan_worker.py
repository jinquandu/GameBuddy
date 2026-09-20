"""私有入口：拾取会话小扫描 worker，由 pickup.pickups 以独立子进程启动。

用法: python3 -m toolbox._pickup_scan_worker <video> <t_lo> <t_hi> <fps> <out_json>
输出 JSON 与 scan_ui_sessions 返回同构（跳变/读数 tuple 序列化为 list）。
"""
import json
import os
import sys

# 必须在 import onnxruntime 之前：多 worker 时每进程限 2 线程，
# N worker × 2 = 核数干净分摊（默认每进程请求全部核会互相拖慢）
os.environ.setdefault("OMP_NUM_THREADS", "2")

if __name__ == "__main__":
    from toolbox.pickup import scan_ui_sessions

    video = sys.argv[1]
    t_lo, t_hi, fps = float(sys.argv[2]), float(sys.argv[3]), float(sys.argv[4])
    out = sys.argv[5]
    sessions = scan_ui_sessions(video, t_lo, t_hi, fps=fps)
    with open(out, "w", encoding="utf-8") as f:
        json.dump(sessions, f, ensure_ascii=False)