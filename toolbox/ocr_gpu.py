"""GPU 批量 OCR：重写 rapidocr 的逐帧推理路径，前后处理复用其实现。

为什么值得（2026-09-21 实测，RTX 3070Ti Laptop + ORT 1.22）：
- PP-OCRv4 det(4.7MB)/rec(10MB) 模型小，单帧 GPU 延迟由 H2D/D2H 传输、
  每帧 session 同步和 Python 开销主导——CUDA EP 单帧 0.22s，其中 GPU
  计算只占几十毫秒；TRT FP16 引擎实测 0.223s/帧，毫无改善；
- 批量推理把开销摊薄：det 一次跑 B 帧（信息带同形天然可堆叠），rec 把
  整批帧的全部文本框合成一两次跑。单 GPU worker 实测 ~3.3 帧/s -> 15+ 帧/s。

约束：run() 的 frames 必须同形（detect 信息带/拾取条带逐视频固定，天然
满足）；模型/字典直接用 rapidocr 自带包内文件，版本升级自动跟随。
"""
import math
import os
from pathlib import Path
from typing import List

import cv2
import numpy as np


def _dll_paths():
    """CUDA/cuDNN(TRT 可选) DLL 目录——torch cu126 自带，免装 CUDA Toolkit。"""
    dirs = []
    try:
        import torch
        dirs.append(str(Path(torch.__file__).parent / "lib"))
    except ImportError:
        pass
    import sys
    base = Path(sys.prefix) / "Lib" / "site-packages"
    for sub in ("tensorrt_libs", "tensorrt_libs/lib"):
        if (base / sub).is_dir():
            dirs.append(str(base / sub))
    return [d for d in dirs if Path(d).is_dir()]


class GpuOCR:
    """批量 GPU OCR。run(frames) -> List[List[(box, text, score)]]。

    box 为 4 点坐标（与 RapidOCR 返回同构，可直接进 detector 的
    _center/位置过滤逻辑）。cls 方向分类跳过——游戏 HUD 文字全水平。
    """

    def __init__(self, det_batch=8, rec_batch=96, use_trt=False,
                 det_limit_side_len=736, det_limit_type="min"):
        for d in _dll_paths():
            os.add_dll_directory(d)
            os.environ["PATH"] = d + os.pathsep + os.environ["PATH"]
        import yaml
        import onnxruntime as ort
        from rapidocr_onnxruntime.ch_ppocr_det.utils import (DBPostProcess,
                                                             DetPreProcess)

        pkg = Path(__import__("rapidocr_onnxruntime").__file__).parent
        cfg = yaml.safe_load((pkg / "config.yaml").read_text(encoding="utf-8"))
        det_cfg = dict(cfg.get("Det") or {})
        rec_cfg = dict(cfg.get("Rec") or {})
        providers = [("CUDAExecutionProvider", {"device_id": 0}),
                     "CPUExecutionProvider"]
        if use_trt and "TensorrtExecutionProvider" in ort.get_available_providers():
            cache = Path.home() / "_trt_ocr_cache"
            cache.mkdir(exist_ok=True)
            trt = ("TensorrtExecutionProvider",
                   {"device_id": 0, "trt_fp16_enable": True,
                    "trt_engine_cache_enable": True,
                    "trt_engine_cache_path": str(cache),
                    "trt_timing_cache_enable": True})
            providers = [trt] + providers
        so = ort.SessionOptions()
        so.intra_op_num_threads = 2
        so.log_severity_level = 4
        det_model = pkg / "models" / "ch_PP-OCRv4_det_infer.onnx"
        self.det_sess = ort.InferenceSession(str(det_model), so,
                                             providers=providers)
        self.det_inp = self.det_sess.get_inputs()[0].name
        # rec 复用 RapidOCR 的 TextRecognizer：ORT CUDA EP 在大批量 rec 上有
        # 病态行为（96框/批实测 2260ms vs 原生 b6 排序分批 397ms，宽度相关
        # 的跨设备同步放大），其排序+小批+按块 pad 是实测最优
        rec_cfg = dict(cfg.get("Rec") or {})
        rec_cfg["model_path"] = str(pkg / "models" / "ch_PP-OCRv4_rec_infer.onnx")
        rec_cfg["use_cuda"] = True
        rec_cfg["intra_op_num_threads"] = 2
        from rapidocr_onnxruntime.ch_ppocr_rec.text_recognize import             TextRecognizer
        self.recognizer = TextRecognizer(rec_cfg)

        self.det_batch = det_batch
        self.rec_batch = rec_batch
        self.drop_score = float(cfg.get("Global", {}).get("drop_score", 0.5))
        # det 前处理（逐帧 resize 到 limit 边长，同形帧输出同形）与 DB 后处理。
        # 细长条带（如拾取容器条 154x418）用 min 策略会被放大成 736x1994 的
        # 巨幅输入（8 帧批量 140MB），条带场景应传 max 策略禁放大
        self.det_pre = DetPreProcess(det_limit_side_len, det_limit_type,
                                     det_cfg.get("mean"),
                                     det_cfg.get("std"))
        self.det_post = DBPostProcess(
            thresh=det_cfg.get("thresh", 0.3),
            box_thresh=det_cfg.get("box_thresh", 0.5),
            max_candidates=det_cfg.get("max_candidates", 1000),
            unclip_ratio=det_cfg.get("unclip_ratio", 1.6),
            use_dilation=det_cfg.get("use_dilation", True),
            score_mode=det_cfg.get("score_mode", "fast"))
        print(f"  [OCR] GPU 批量模式 det_b={det_batch} rec_b={rec_batch}"
              f"{' +TRT' if use_trt and len(providers) > 2 else ''}",
              flush=True)

    # ---------------------------------------------------------------- det
    def _det_batch(self, frames):
        """同形帧堆成一个 batch 过 det 网，返回每帧的 (boxes, scores)。"""
        processed = [self.det_pre(f) for f in frames]
        # DetPreProcess 输出 (1,3,H,W) float32；同形输入输出同形，直接堆叠
        blob = np.concatenate([p if p.ndim == 4 else p[np.newaxis]
                               for p in processed], axis=0)
        preds = self.det_sess.run(None, {self.det_inp: blob})[0]
        out = []
        h, w = frames[0].shape[:2]
        for i in range(len(frames)):
            boxes, scores = self.det_post(preds[i:i + 1], (h, w))
            out.append(self._filter(boxes, (h, w)))
        return out

    @staticmethod
    def _filter(dt_boxes, image_shape):
        """复刻 TextDetector.filter_tag_det_res：点序规整 + 越界裁剪 + 过小框剔除。"""
        def order_clockwise(pts):
            s = pts.sum(axis=1)
            diff = np.diff(pts, axis=1).reshape(-1)
            return np.array([pts[np.argmin(s)], pts[np.argmin(diff)],
                             pts[np.argmax(s)], pts[np.argmax(diff)]],
                            dtype=np.float32)
        h, w = image_shape
        keep = []
        for box in dt_boxes:
            box = order_clockwise(box)
            box[:, 0] = np.clip(box[:, 0], 0, w)
            box[:, 1] = np.clip(box[:, 1], 0, h)
            rect_w = int(np.linalg.norm(box[0] - box[1]))
            rect_h = int(np.linalg.norm(box[0] - box[3]))
            if rect_w <= 3 or rect_h <= 3:
                continue
            keep.append(box)
        return np.array(keep) if keep else np.zeros((0, 4, 2), np.float32)

    # ---------------------------------------------------------------- 裁剪
    @staticmethod
    def _crop(frame, box):
        """get_rotate_crop_image 简版（水平四点框 → 透视校正裁剪）。"""
        pts = box.astype(np.float32)
        w = int(max(np.linalg.norm(pts[0] - pts[1]), np.linalg.norm(pts[3] - pts[2])))
        h = int(max(np.linalg.norm(pts[0] - pts[3]), np.linalg.norm(pts[1] - pts[2])))
        if w < 2 or h < 2:
            return None
        dst = np.float32([[0, 0], [w, 0], [w, h], [0, h]])
        M = cv2.getPerspectiveTransform(pts, dst)
        return cv2.warpPerspective(frame, M, (w, h))

    # ---------------------------------------------------------------- rec
    def _resize_norm(self, img, max_wh_ratio):
        img_c, img_h, img_w = self.rec_img_shape
        img_w = int(img_h * max_wh_ratio)
        h, w = img.shape[:2]
        ratio = w / float(h)
        resized_w = min(img_w, int(math.ceil(img_h * ratio)))
        resized = cv2.resize(img, (resized_w, img_h)).astype("float32")
        resized = resized.transpose((2, 0, 1)) / 255
        resized = (resized - 0.5) / 0.5
        pad = np.zeros((img_c, img_h, img_w), dtype=np.float32)
        pad[:, :, 0:resized_w] = resized
        return pad

    def _rec_batch(self, crops):
        """全部裁剪图过 rec（TextRecognizer，返回与入参同序）。"""
        if not crops:
            return []
        res, _ = self.recognizer(crops)
        return res

    # ---------------------------------------------------------------- 入口
    def run(self, frames: List[np.ndarray]):
        """同形 BGR 帧列表 -> 每帧 [(box, text, score)]（与 RapidOCR 同构）。"""
        if not frames:
            return []
        per_frame_boxes = self._det_batch(frames)
        entries = []                    # (帧号, 框, 裁剪图)；裁剪失败的框不入列
        for fi, frame in enumerate(frames):
            for box in per_frame_boxes[fi]:
                crop = self._crop(frame, box)
                if crop is not None:
                    entries.append((fi, box, crop))
        texts = self._rec_batch([e[2] for e in entries]) if entries else []
        out = [[] for _ in frames]
        for (fi, box, _), tr in zip(entries, texts):
            if float(tr[1]) < self.drop_score:   # 与 RapidOCR 低分读数过滤对齐
                continue
            out[fi].append((box, tr[0], tr[1]))
        return out
