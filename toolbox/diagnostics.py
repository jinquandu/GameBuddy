"""脱敏诊断信息(REQ-20261022-010 A 部分)。

用途:客户遇到问题时,点「复制诊断信息」得到一段可读文本发给客服;同时
写入 data/diagnostics/last-report.txt 覆盖保存。**只导出可核实的既有字段**,
不含 bearer、短信验证码、完整手机号、微信原始 ID、设备私钥。

字段来源:
- 版本/构建:toolbox.version;
- 设备展示码:device_identity.device_code();
- 登录/权益/绑定:account.prerequisites()(失败则记「未知」);
- 环境检测:envcheck.last_result();
- 本地队列:folder_watch.jobs_view()/summary();
- 云端成片:pet 传入的 produce 快照(C2S-8 的 produce 字段)。
"""
import json
import re
import time
from pathlib import Path

from toolbox.config import DATA

REPORT_DIR = DATA / "diagnostics"
REPORT_FILE = REPORT_DIR / "last-report.txt"

_PHONE = re.compile(r"(?<!\d)(1[3-9]\d)(\d{4})(\d{4})(?!\d)")
_TOKENISH = re.compile(r"(sk-[A-Za-z0-9]{8,}|Bearer\s+[A-Za-z0-9._\-]{8,})")


def mask_phone(text):
    """138****1234;不动其他数字(时间/GB 等)。"""
    return _PHONE.sub(lambda m: m.group(1) + "****" + m.group(3), str(text))


def redact(text):
    """兜底脱敏:手机号 + 形似令牌的串。"""
    return _TOKENISH.sub("<已脱敏>", mask_phone(text))


def _mask_uid(uid):
    s = str(uid or "")
    return s if len(s) <= 10 else s[:6] + "…" + s[-4:]


def build(version_label, sess=None, pre=None, env=None, prod=None,
          jobs=None, summary=None, produce=None, extra_lines=None):
    """返回诊断文本(已脱敏)。所有入参缺失都按「未知」输出,不编造。"""
    lines = [f"陪玩大师姐诊断 - {time.strftime('%Y-%m-%d %H:%M:%S')}",
             f"版本：{version_label}"]
    s = sess or {}
    if s.get("user_id"):
        lines.append(f"账号：{_mask_uid(s.get('user_id'))}"
                     f" · 手机 {mask_phone(s.get('phone') or '未知')}")
    else:
        lines.append("账号：未登录")
    p = pre or {}
    tri = {True: "✓", False: "✗", None: "未知"}
    lines.append(
        "前置：登录 " + tri.get(p.get("logged_in"))
        + " · 权益 " + tri.get(p.get("member"))
        + " · 微信 " + {"bound": "✓", "none": "✗"}.get(p.get("binding"), "未知")
        + " · 通知 " + (tri.get(p.get("opt_in")) if p.get("opt_in") is not None
                        else "未知")
        + " · 环境 " + {"pass": "✓", "warn": "!", "fail": "✗",
                        "off": "未检测"}.get((env or {}).get("_state", "off"),
                                             "未知"))
    e = env or {}
    if e:
        est = (e.get("estimate") or {}).get("min_per_30")
        lines.append(f"环境检测：{'通过' if e.get('ok') else '未通过'}"
                     f" · 提示 {e.get('warn', 0)} 项"
                     + (f" · 预计 30 分钟录像 {est:g} 分钟识别" if est else "")
                     + ("" if not e.get("incomplete") else " · 检测未完成"))
        for it in e.get("items", []):
            lines.append(f"  [{it.get('level')}] {it.get('label')}："
                         f"{it.get('detail')}")
    else:
        lines.append("环境检测：未检测")
    pr = prod or {}
    wd = pr.get("wdir") or ""
    lines.append(f"录像目录：{wd or '未选择'}")
    lines.append(f"最近生产时刻：{_fmt_ts(pr.get('last_ts'))}"
                 f" · 下次检查：{_fmt_ts(pr.get('next_check_ts'))}")
    sm = summary or {}
    lines.append(f"本地队列：排队 {sm.get('queued', 0)} · 处理中 "
                 f"{sm.get('running', 0)} · 完成 {sm.get('done', 0)} · 失败 "
                 f"{len(sm.get('failed', []))}")
    for j in (jobs or [])[:12]:
        lines.append(f"  - [{j.get('state')}] {j.get('session')}"
                     + (f" · {j.get('note')}" if j.get("note") else ""))
    if isinstance(produce, dict) and produce:
        lines.append("云端成片：" + json.dumps(
            {k: produce.get(k) for k in ("session", "state", "error")
             if produce.get(k) is not None}, ensure_ascii=False))
    else:
        lines.append("云端成片：未知（无本机记录）")
    lines.append("通知送达：未知（通知服务未提供查询）")
    for ln in (extra_lines or []):
        lines.append(str(ln))
    return redact("\n".join(lines))


def _fmt_ts(ts):
    try:
        ts = float(ts or 0)
    except (TypeError, ValueError):
        return "无"
    if ts <= 0:
        return "无"
    return time.strftime("%m-%d %H:%M", time.localtime(ts))


def save(text):
    """覆盖写 data/diagnostics/last-report.txt;返回路径或 None。"""
    try:
        REPORT_DIR.mkdir(parents=True, exist_ok=True)
        tmp = REPORT_FILE.with_suffix(".tmp")
        tmp.write_text(text, encoding="utf-8")
        tmp.replace(REPORT_FILE)
        return REPORT_FILE
    except OSError:
        return None


def _selftest_guard(text):
    """供测试/发布检查:断言不含完整手机号或令牌。"""
    return bool(_PHONE.search(text) or _TOKENISH.search(text))
