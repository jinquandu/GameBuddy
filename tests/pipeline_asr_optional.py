"""pipeline 语音线可选化自测：_voice_line_ready 探测逻辑（无网络/无模型）。"""
import sys
from unittest import mock

sys.path.insert(0, ".")

from toolbox.pipeline import _voice_line_ready

FAIL = []


def check(name, cond):
    print(("PASS " if cond else "FAIL ") + name)
    if not cond:
        FAIL.append(name)


def fake_spec(installed):
    """find_spec 替身：installed 集合内的回真值，其余回 None。"""
    def _f(name):
        return object() if name in installed else None
    return _f


ALL = {"funasr", "torch", "torchaudio", "sklearn", "librosa"}

# ---- 1) 开关关闭 -> 整线不可用（不看依赖）----
ok, why = _voice_line_ready({"features": {"asr": False}})
check("开关关闭 -> 不可用", (ok, why) == (False, "features.asr=false"))

# ---- 2) 缺任一依赖 -> 不可用，原因点名缺失包（核心版场景）----
with mock.patch("importlib.util.find_spec", fake_spec(ALL - {"torch"})):
    ok, why = _voice_line_ready({})
check("缺 torch -> 不可用+点名", ok is False and "torch" in why)

with mock.patch("importlib.util.find_spec", fake_spec(set())):
    ok, why = _voice_line_ready({"features": {"asr": True}})
check("全缺 -> 不可用", ok is False)

# ---- 3) 依赖齐全 + 开关开（含缺省配置）-> 可用 ----
with mock.patch("importlib.util.find_spec", fake_spec(ALL)):
    ok, why = _voice_line_ready({})
check("依赖齐+开关开 -> 可用", (ok, why) == (True, ""))

# ---- 4) 本机实况（完整版应可用；核心版机器此步为 INFO 参考值）----
ok, why = _voice_line_ready({})
print(f"INFO 本机语音线：ok={ok} why={why}")

print(f"\n{'ALL PASS' if not FAIL else 'FAILED: ' + ', '.join(FAIL)}")
sys.exit(1 if FAIL else 0)
