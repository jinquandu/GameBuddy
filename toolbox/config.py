"""配置加载与数据目录约定。

config.yaml（可选）与 config.example.yaml 同级，用户配置深度覆盖默认值。
数据根支持三种指定方式（优先级从高到低）：
1. 环境变量 TOOLBOX_DATA_DIR（服务端容器用，绝对路径挂载点）
2. config.yaml 的 paths.data_dir（相对项目根或绝对路径）
3. 默认 <项目根>/data

应用端（Mac）用默认值即可；服务端容器以 TOOLBOX_DATA_DIR 指向
/opt/dashijie-eval-data，同一套代码在两端读写各自的数据根。
"""
import copy
import os
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent

TEMPLATES_DIR = Path(__file__).resolve().parent / "templates"
FONTS_DIR = Path(__file__).resolve().parent / "fonts"

DEFAULTS = {
    "paths": {"data_dir": "data"},
    "highlight": {
        "method": "keyword",       # keyword=ASR+关键词；llm=大模型选段（后续）
        "preroll_sec": 0,
        "max_clip_sec": 180,
    },
    "remix": {"template": "default", "font_path": ""},
    "knockdown": {"pre_sec": 6, "post_sec": 10, "merge_sec": 12},
    "pickup": {"merge_sec": 25, "look_back": 35, "look_fwd": 15,
               "max_sec": 90, "scan_workers": 5},
    "douyin": {
        "mode": "openapi",         # openapi=官方开放平台；browser=浏览器自动化（均不实装，见 douyin.py）
        "credentials_file": "douyin_credentials.yaml",
        "publish_schedule": "",
    },
    "stats": {"method": "ocr", "game": "pubg"},
    "llm": {"enabled": False},
    "download": {
        "cookies_file": "bilibili_cookies.txt",   # Netscape 格式 cookie jar
        "qn": 80,                                  # 清晰度：80=1080P
    },
    "recorder": {
        # ---- B站直播录制（toolbox/recorder.py，python3 -m toolbox record）----
        "qn": 10000,         # 画质档：10000=原画(1080P)；房间不支持时自动降最高可用档
        "segment_min": 30,   # 单分片时长（分钟），到点轮转，P 号延续
        "stall_sec": 20,     # 录制无数据超时（秒），判定断流重拉直链续录
        "poll_sec": 30,      # watch 模式未开播时的轮询间隔（秒）
    },
    "server": {
        # ---- 应用端 <-> 服务端（ECS）搬运配置，见 toolbox/transfer.py ----
        "ssh_alias": "ecs",                        # ~/.ssh/config 里的别名
        "remote_root": "/opt/dashijie-eval-data", # 服务端稳定数据根（部署包外，更新不删）
        "tenant": "default",                       # 租户（多租户方案A：数据按租户分前缀）
        "container": "dashijie-eval",              # 服务端跑 toolbox 的容器名
        "site_url": "https://95188.pw/eval/",     # 打标站地址
        "keep_sessions": 3,                       # 服务端保留最近 N 个会话（prune 默认值）
    },
}


def _merge(base, override):
    """深度合并：override 的标量/子树覆盖 base。"""
    out = copy.deepcopy(base)
    for k, v in (override or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge(out[k], v)
        else:
            out[k] = v
    return out


def _user_config():
    cfg_file = ROOT / "config.yaml"
    if cfg_file.exists():
        return yaml.safe_load(cfg_file.read_text(encoding="utf-8")) or {}
    return {}


def load_config():
    """读取项目根的 config.yaml（不存在则全默认值）与 DEFAULTS 深度合并。"""
    return _merge(DEFAULTS, _user_config())


def _resolve_data_dir(cfg):
    """数据根解析：TOOLBOX_DATA_DIR 环境变量 > paths.data_dir > 默认 data。"""
    env = os.environ.get("TOOLBOX_DATA_DIR")
    raw = env if env else (cfg.get("paths") or {}).get("data_dir") or "data"
    p = Path(str(raw)).expanduser()
    return p if p.is_absolute() else (ROOT / p)


# 数据根在 import 时定死：各模块 `from toolbox.config import DATA` 直接可用；
# 服务端容器配置一次 TOOLBOX_DATA_DIR 后整套链路自然落到挂载点。
DATA = _resolve_data_dir(load_config())
RECORDINGS = DATA / "recordings"   # 原始视频 + index.json
HIGHLIGHTS = DATA / "highlights"   # data/highlights/<video_id>/
REMIXES = DATA / "remixes"         # 混剪成片
REPORTS = DATA / "reports"         # stats.json + events.jsonl + report.md
ASR = DATA / "asr"                 # transcript.json / speaker_labeled.json / audio_16k.wav

INDEX_FILE = RECORDINGS / "index.json"


def ensure_dirs():
    for d in (RECORDINGS, HIGHLIGHTS, REMIXES, REPORTS):
        d.mkdir(parents=True, exist_ok=True)