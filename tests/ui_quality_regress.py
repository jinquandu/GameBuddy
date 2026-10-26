# -*- coding: utf-8 -*-
"""REQ-20261022-006/007/008/009/010 回归。

- DPI(006):setup 幂等、scale 计算、px 换算、非 win32 降级
- 组件(007):间距基线/字体三档/按钮语义/图标绘制/滚动容器/空态
- 状态(008):云端未确认不虚报已出片、等待原因、失败原因、暂停规则文案
- 版本(009):版本标签、构建号、界面可见
- 诊断(010):脱敏(手机号/令牌)、字段齐全、落盘、不含敏感值
"""
import re
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, ".")

from toolbox import diagnostics, dpi, ui_kit, version

ok = 0


def check(cond, what):
    global ok
    assert cond, "FAIL: " + what
    ok += 1
    print("  ✓ " + what)


# ---------------------------------------------------------------- DPI(006)
st1 = dpi.setup()
st2 = dpi.setup()
check(st1 == st2, "DPI:setup 幂等")
check(dpi.scale() >= 1.0, f"DPI:缩放比合理({dpi.scale():.2f})")
check(dpi.px(10) == int(round(10 * dpi.scale())), "DPI:px() 按缩放换算")
check(dpi.px(0) == 0, "DPI:px(0)=0")
check(set(st1) == {"awareness", "dpi", "scale"}, "DPI:状态字段齐全")
check(st1["dpi"] % 96 == 0 or st1["dpi"] > 0, "DPI:系统 DPI 可读")

# ---------------------------------------------------------------- 组件(007)
check((ui_kit.S1, ui_kit.S2, ui_kit.S3, ui_kit.S4) == (4, 8, 12, 16),
      "组件:8px 间距基线")
check(len({ui_kit.F_TITLE, ui_kit.F_BODY, ui_kit.F_SMALL}) == 3, "组件:三档字体层级")
for key in ("primary", "deep", "cta", "lav", "line", "danger", "good",
            "warn", "dim", "fg", "card", "page"):
    check(key in ui_kit.BRAND, f"组件:品牌色 {key}")
check(ui_kit.BRAND["danger"] != ui_kit.BRAND["primary"], "组件:危险色独立于主色")

import tkinter as tk
root = tk.Tk()
root.withdraw()
try:
    ui_kit.setup_style(root)
    btn = ui_kit.button(root, "x", kind="cta")
    check(btn.cget("style") == "Cta.TButton", "组件:按钮语义样式(cta)")
    dang = ui_kit.button(root, "删除", kind="danger")
    check(dang.cget("style") == "Danger.TButton", "组件:危险按钮样式")
    c = ui_kit.icon(root, "home", ui_kit.BRAND["primary"], "white")
    check(len(c.find_all()) > 0, "组件:矢量图标已绘制")
    sf = ui_kit.ScrollFrame(root, "white", height=80)
    check(sf.canvas.winfo_exists() and sf.vbar.winfo_exists(),
          "组件:滚动容器含 canvas+滚动条")
    es = ui_kit.empty_state(root, "还没有真实成片", "录像足够后自动开始")
    check(es.winfo_exists(), "组件:空态可构建")
finally:
    root.destroy()

# ---------------------------------------------------------------- 版本(009)
lab = version.label()
check(version.VERSION in lab and version.BUILD in lab, "版本:标签含版本与构建号")
check(re.match(r"^\d+\.\d+(\.\d+)*$", version.VERSION) is not None,
      "版本:版本号格式(对外恒定 0.1;内部曾用 x.y.z)")
check(version.display() == "V0.1",
      "版本:对外恒定显示 V0.1(用户指示 2026-10-26)")
pet_src = Path("toolbox/pet.py").read_text(encoding="utf-8")
check("version.display()" in pet_src
      and "v{version.VERSION}" not in pet_src,
      "版本:界面一律走 display(),不直接拼内部版本")
check(re.match(r"^\d{8}-r\d+[a-z]*$", version.BUILD) is not None, "版本:构建号格式(允许 rN/ rNa 重打后缀)")

# ---------------------------------------------------------------- 诊断(010)
sample = diagnostics.build(
    version.label(),
    sess={"user_id": "dsj_6d647af2f514a1b7", "phone": "17621195726"},
    pre={"logged_in": True, "member": True, "member_expires": None,
         "binding": "bound", "opt_in": True},
    env={"ok": True, "warn": 0, "checked_at": "2026-10-22 15:26:28",
         "items": [{"key": "ocr", "label": "画面识别", "level": "ok",
                    "detail": "0.3 秒/帧"}],
         "estimate": {"min_per_30": 2.1}},
    prod={"wdir": "D:/录像", "last_ts": time.time(), "next_check_ts": 0},
    jobs=[{"key": "k", "session": "场次A", "state": "queued", "note": ""}],
    summary={"queued": 1, "running": 0, "done": 0, "failed": []},
    produce={"场次A": {"state": "waiting", "ts": time.time()}},
)
check("17621195726" not in sample, "诊断:完整手机号不出现")
check("176****5726" in sample, "诊断:手机号已脱敏显示")
check("dsj_6d647af2f514a1b7" not in sample, "诊断:内部账号 ID 已截断")
check("Bearer" not in sample and "sk-" not in sample, "诊断:不含令牌")
check("版本：" in sample and version.BUILD in sample, "诊断:含版本与构建号")
check("云端成片" in sample and "通知送达：未知" in sample,
      "诊断:云端与通知诚实标注")
check("环境检测：通过" in sample, "诊断:含环境结论")
check(diagnostics._selftest_guard(sample) is False, "诊断:守卫判定为已净化")

# 守卫本身要能认出敏感值(负样例),而 build() 出来的文本已被兜底脱敏
check(diagnostics._selftest_guard("手机 13900001111") is True,
      "诊断:守卫识别裸手机号(负样例)")
check(diagnostics._selftest_guard("token=sk-abcdefghijklmnop") is True,
      "诊断:守卫识别裸令牌(负样例)")
leak = diagnostics.build(version.label(),
                         sess={"phone": "13900001111"},
                         extra_lines=["token=sk-abcdefghijklmnop"])
check(diagnostics._selftest_guard(leak) is False,
      "诊断:build 已兜底脱敏(含敏感输入也不漏)")

with tempfile.TemporaryDirectory() as td:
    real_dir, real_file = diagnostics.REPORT_DIR, diagnostics.REPORT_FILE
    diagnostics.REPORT_DIR = Path(td) / "diagnostics"
    diagnostics.REPORT_FILE = diagnostics.REPORT_DIR / "last-report.txt"
    try:
        p = diagnostics.save(sample)
        check(p is not None and p.is_file(), "诊断:落盘成功")
        check(p.read_text(encoding="utf-8").startswith("陪玩大师姐诊断"),
              "诊断:文件内容正确")
        diagnostics.save("第二次")
        check(p.read_text(encoding="utf-8") == "第二次", "诊断:覆盖写(不累积)")
    finally:
        diagnostics.REPORT_DIR, diagnostics.REPORT_FILE = real_dir, real_file

# ---------------------------------------------------------------- 状态文案(008)
pet_src = Path("toolbox/pet.py").read_text(encoding="utf-8")
check("结果待确认" in pet_src, "状态:存在「结果待确认」态")
check("云端生成中" in pet_src, "状态:存在「云端生成中」态")
check("选定后当天不能换" not in pet_src,
      "状态:已移除暂停规则(选后不能换)的承诺文案")
check("3 条候选" in pet_src or "3 条候选" in pet_src, "状态:候选表述保留")
check("明天还会继续" not in pet_src, "状态:移除「明天还会继续」无条件承诺")
check("候选在我的作品" not in pet_src, "状态:移除「候选在我的作品」无结果指引")
check("_wait_reason" in pet_src, "状态:队列有等待原因函数")
check("复制诊断信息" in pet_src, "状态:界面提供诊断入口")

fw_src = Path("toolbox/folder_watch.py").read_text(encoding="utf-8")
check("PIPELINE_STAGES" in fw_src and "画面识别" in fw_src, "状态:跑链阶段中文名")
check("_progress_line" in fw_src and "running_progress" in fw_src,
      "状态:跑链阶段进度登记")
check("scan_stats" in fw_src, "状态:目录扫描摘要")
check("next_scan_ts" in fw_src and "mark_scan" in fw_src, "状态:下次检查时刻")

# ---------------------------------------------------------------- 吊销留痕(008 验收)
from toolbox import device_identity as di
import tempfile as _tf
def _isolate_device(td):
    """四个设备路径全指向临时目录——只改 DEVICE_DIR 会让 save_session 写穿
    到真实会话文件(2026-10-22 实测踩过)。"""
    real = (di.DEVICE_DIR, di.KEY_FILE, di.SESSION_FILE, di.REVOKE_FILE)
    d = Path(td)
    di.DEVICE_DIR = d
    di.KEY_FILE = d / "pc-device-v1.dpapi"
    di.SESSION_FILE = d / "session.json"
    di.REVOKE_FILE = d / "session_revoked"
    return real


def _restore_device(real):
    di.DEVICE_DIR, di.KEY_FILE, di.SESSION_FILE, di.REVOKE_FILE = real


with _tf.TemporaryDirectory() as td:
    real_dev = _isolate_device(td)
    try:
        check(di.revoked() == {}, "吊销留痕:初始为空")
        di.mark_revoked("device_revoked")
        m = di.revoked()
        check(m.get("code") == "device_revoked" and m.get("at"),
              "吊销留痕:写入错误码与时间(不含令牌)")
        raw = di.REVOKE_FILE.read_text(encoding="utf-8")
        check("bearer" not in raw and "token" not in raw.lower(),
              "吊销留痕:文件不含令牌")
        di.save_session({"user_id": "u", "bearer": "t",
                         "expires_at_ts": time.time() + 3600})
        check(di.revoked() == {}, "吊销留痕:重新登录(保存会话)即清除")
        di.mark_revoked("invalid_token")
        di.clear_revoked()
        check(di.revoked() == {}, "吊销留痕:主动清除生效")
    finally:
        _restore_device(real_dev)

# 无会话但留痕 → prerequisites 继续报「已失效」,不倒退为「未登录」
from toolbox.account import AccountClient
from toolbox.config import load_config
with _tf.TemporaryDirectory() as td:
    real_dev = _isolate_device(td)
    di.mark_revoked("device_revoked")
    try:
        a = load_config().get("account") or {}
        pre = AccountClient(a.get("api_url") or "http://127.0.0.1:1",
                            None).prerequisites()
        check(pre["logged_in"] is False
              and pre["auth_error"] == "device_revoked"
              and "重新登录" in pre["auth_message"],
              "吊销留痕:无会话时仍报已失效(不倒退)")
    finally:
        _restore_device(real_dev)

print("PASS: " + str(ok) + " 项断言全部通过")
