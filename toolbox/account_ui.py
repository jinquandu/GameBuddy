"""账号登录对话框（REQ-20261019-002 B1~B3；契约 §4A C2S-10/11）。

素朴紧凑（用户 UI 偏好）：手机号+验证码两行、两个按钮、一条状态。
关键行为：
- 打开即做能力门控（C2S-11）：服务未声明 pc_device_auth_contract=1.0 →
  显示"服务待就绪"并禁用输入（不降级、不绕过、不回退旧通道）；
- 409 device_bound：弹确认框，用户显式同意才 replace_device=true 重试
  （重新完成短信验证由服务端语义保证），不自动抢占；
- 登录成功回填会话，主窗状态行由调用方刷新。

错误路径铁律（REQ-20261019-014 P0）：后台线程不直接碰 Tk（一律
win.after 回主线程）；except 块里的异常变量出块即被 Python 清除，
**禁止闭包引用**——先转成安全文本再排回调；失败/超时必须给出明确
结果并恢复按钮，不无限停留"发送中"；异常文案带脱敏诊断（阶段/
错误码/HTTP 状态/耗时），手机号、验证码、token 不进任何日志。
"""
import threading
import time
import tkinter as tk
from tkinter import ttk, messagebox

from toolbox import device_identity as di
from toolbox.account import (AccountClient, AccountError, ServiceNotReady)


def _err_text(stage, exc, t0):
    """异常 → 安全文本（出 except 块前转字符串，杜绝闭包引用已清除的
    异步异常变量），带脱敏诊断（错误码/HTTP 状态/耗时）。"""
    dur = int((time.time() - t0) * 1000)
    if isinstance(exc, AccountError):
        head = f"{stage}失败：{exc}"
        code, status = exc.code, exc.status
    else:
        head = f"{stage}异常：{exc!r}"
        code, status = "unexpected", ""
    return head + f"（{code}" + (f" HTTP{status}" if status else "") \
        + f" · {dur}ms）"


def login_dialog(master, api_url, production_url=None) -> dict:
    """模态登录；返回会话 dict（成功）或 {}（取消/未就绪）。"""
    win = tk.Toplevel(master)
    win.title("账号登录")
    win.resizable(False, False)
    win.configure(bg="#F6F5F2")
    win.transient(master)
    f = ttk.Frame(win, padding=(14, 12))
    f.pack(fill="both", expand=True)

    ttk.Label(f, text="手机号").pack(anchor="w")
    phone_var = tk.StringVar()
    phone_e = ttk.Entry(f, textvariable=phone_var, width=22)
    phone_e.pack(fill="x", pady=(1, 8))
    ttk.Label(f, text="短信验证码").pack(anchor="w")
    code_var = tk.StringVar()
    code_e = ttk.Entry(f, textvariable=code_var, width=22)
    code_e.pack(fill="x", pady=(1, 8))
    row = ttk.Frame(f)
    row.pack(fill="x")
    sms_btn = ttk.Button(row, text="获取验证码", width=10)
    login_btn = ttk.Button(row, text="登录", width=8)
    sms_btn.pack(side="left", padx=(0, 6))
    login_btn.pack(side="left")
    st = tk.Label(f, text="检查服务能力…", bg="#F6F5F2", fg="#8A8A85",
                  font=("Microsoft YaHei UI", 9), wraplength=230, justify="left")
    st.pack(fill="x", pady=(8, 0))
    ttk.Label(f, text=f"本机设备 {di.device_code()}（换机需重新验证）",
              foreground="#B0B0AA",
              font=("Microsoft YaHei UI", 8)).pack(anchor="w", pady=(6, 0))

    cli = AccountClient(api_url, production_url)
    result = {"sess": {}, "challenge": None}
    busy = {"on": False}

    def say(text, warn=False):
        st.config(text=text, fg="#C0392B" if warn else "#5A7A4F")

    def set_enabled(on):
        for w in (phone_e, code_e, sms_btn, login_btn):
            w.config(state="normal" if on else "disabled")

    def check():
        """能力门控（C2S-11）——后台线程执行，主线程改 UI，不冻窗。"""
        def run():
            t0 = time.time()
            try:
                cli.ensure_pc_auth()
                _main(lambda: (say("服务就绪，请输入手机号"), set_enabled(True),
                               phone_e.focus_set()))
            except AccountError as e:
                dur = int((time.time() - t0) * 1000)
                txt = ("账号服务待就绪" if isinstance(e, ServiceNotReady)
                       else f"服务不可达：{e}")
                txt += f"（{e.code}" + (f" HTTP{e.status}" if e.status else "") \
                    + f" · {dur}ms），稍后再试"
                _main(lambda m=txt: (say(m, warn=True), set_enabled(False)))
        threading.Thread(target=run, daemon=True).start()

    def _bg(fn):
        if busy["on"]:
            return
        busy["on"] = True
        set_enabled(False)

        def wrap():
            try:
                fn()
            finally:
                busy["on"] = False
                win.after(0, lambda: set_enabled(True))
        threading.Thread(target=wrap, daemon=True).start()

    def _main(fn):
        """后台线程 → 主线程（Tk 只在主线程动）。"""
        win.after(0, fn)

    def _fail(stage, exc, t0):
        msg = _err_text(stage, exc, t0)
        _main(lambda m=msg: say(m, warn=True))

    def do_sms():
        phone = phone_var.get().strip()
        if not phone.isdigit() or len(phone) != 11:
            say("手机号不对呀（11 位数字）", warn=True)
            return

        def run():
            t0 = time.time()
            _main(lambda: say("发送验证码…"))
            try:
                r = cli.request_sms(phone)
                result["challenge"] = r.get("challenge_id")
                ok = bool(result["challenge"])
                msg = ("验证码已发送，查收短信" if ok
                       else "服务未返回验证凭据（challenge_id），稍后再试")
                _main(lambda m=msg, k=ok: say(m, warn=not k))
            except AccountError as e:
                _fail("发送", e, t0)
            except Exception as ex:            # noqa: BLE001 未知异常也要落地
                _fail("发送", ex, t0)
        _bg(run)

    def do_login():
        phone = phone_var.get().strip()
        code = code_var.get().strip()
        if not result.get("challenge"):
            say("先点「获取验证码」拿到短信凭据再登录", warn=True)
            return

        def run(replace=False):
            t0 = time.time()
            _main(lambda m=f"登录中…{('（换机确认）') if replace else ''}":
                  say(m))
            try:
                sess = cli.login(result["challenge"], code, phone=phone,
                                 replace_device=replace)
                result["sess"] = sess
                _main(win.destroy)
            except AccountError as e:
                if e.code == "device_bound":
                    bound = (e.data.get("bound_device") or {})
                    msg = ("该账号已绑定其他电脑（%s）。\n换绑会使旧电脑登录失效，"
                           "确定换绑到本机吗？" % bound.get("digest", "未知设备"))
                    # 确认框回主线程；同意后再回后台线程重试（replace=True）
                    def ask():
                        if messagebox.askyesno("换机确认", msg, parent=win):
                            _bg(lambda: run(True))
                        else:
                            say("已取消换绑")
                    _main(ask)
                    return
                _fail("登录", e, t0)
            except Exception as ex:            # noqa: BLE001
                _fail("登录", ex, t0)
        _bg(lambda: run(False))

    set_enabled(False)                 # 能力确认前输入一律禁用（check 放行）
    sms_btn.config(command=do_sms)
    login_btn.config(command=do_login)
    win.protocol("WM_DELETE_WINDOW", win.destroy)
    win.wait_visibility()
    win.grab_set()
    win.after(50, check)
    master.wait_window(win)
    return result["sess"]
