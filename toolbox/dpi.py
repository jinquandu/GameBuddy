"""DPI 适配(REQ-20261022-006):进程 DPI 感知 + Tk 缩放基线。

为什么需要:Windows 在 125%/150% 缩放下,若进程声明为 DPI unaware,
系统会把整窗按位图拉伸——文字发虚、间距失真。正确做法是**创建任何窗口
之前**先把进程声明为 DPI 感知,再按真实 DPI 设置 Tk 缩放(tk scaling),
并用 px() 把布局里的逻辑像素换算成物理像素。

模式选择:System DPI Aware(1)。理由:Tk 8.6 不处理跨屏 WM_DPICHANGED
(Per-Monitor 会在拖动到不同缩放显示器后错位),而客户机绝大多数是单屏;
System Aware 在任意系统缩放(100/125/150/175/200%)下都由系统按主屏 DPI
渲染,文字清晰且布局稳定。双屏不同缩放场景按 REQ 约定记阻断,不冒充通过。

约束:
- setup() 必须在 tkinter 创建任何窗口之前调用(本模块只做进程声明,
  不建窗口);
- 一旦声明成功即幂等,重复调用返回同一状态;
- 非 Windows 平台 scale=1.0,行为不变。
"""
import ctypes
import sys

from toolbox.logs import get as _getlog

LOG = _getlog("pet")

_STATE = {"awareness": None, "dpi": 96, "scale": 1.0}


def _set_awareness():
    """-> 1=System Aware / 2=Per-Monitor / 0=unaware。先系统级,失败再降级。"""
    try:
        if ctypes.windll.shcore.SetProcessDpiAwareness(1) == 0:
            return 1
    except (AttributeError, OSError):
        pass
    try:
        ctypes.windll.user32.SetProcessDPIAware()
        return 1
    except (AttributeError, OSError):
        return 0


def _system_dpi():
    try:
        dpi = int(ctypes.windll.user32.GetDpiForSystem())
        return dpi or 96
    except (AttributeError, OSError):
        pass
    try:                                   # Win7/8 回退:取屏幕 DC 的 LOGPIXELSX
        hdc = ctypes.windll.user32.GetDC(0)
        try:
            return int(ctypes.windll.gdi32.GetDeviceCaps(hdc, 88)) or 96
        finally:
            ctypes.windll.user32.ReleaseDC(0, hdc)
    except (AttributeError, OSError):
        return 96


def setup():
    """声明 DPI 感知并算出缩放比;幂等。返回 {awareness,dpi,scale}。"""
    if _STATE["awareness"] is not None:
        return _STATE
    if sys.platform != "win32":
        _STATE.update(awareness=0, dpi=96, scale=1.0)
        return _STATE
    aware = _set_awareness()
    dpi = _system_dpi()
    _STATE.update(awareness=aware, dpi=dpi, scale=dpi / 96.0)
    LOG.info("DPI: awareness=%s dpi=%d scale=%.3f", aware, dpi,
             _STATE["scale"])
    return _STATE


def state():
    return dict(_STATE)


def scale():
    return float(_STATE["scale"])


def px(n):
    """逻辑像素 → 物理像素(四舍五入,最小 0)。"""
    return int(round(float(n) * _STATE["scale"]))


def apply_tk(root):
    """把 Tk 的字号缩放对齐到系统 DPI(字号为 point 时按此换算)。"""
    st = setup()
    try:
        root.tk.call("tk", "scaling", st["dpi"] / 72.0)
    except Exception as e:                 # noqa: BLE001 Tk 版本差异不致命
        LOG.info("tk scaling 设置跳过:%s", e)
    return st
