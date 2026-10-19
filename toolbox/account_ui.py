"""账号登录对话框（REQ-20261019-002 B1~B3；契约 §4A C2S-10/11）。

素朴紧凑（用户 UI 偏好）：手机号+验证码两行、两个按钮、一条状态。
关键行为：
- 打开即做能力门控（C2S-11）：服务未声明 pc_device_auth_contract=1.0 →
  显示"服务待就绪"并禁用输入（不降级、不绕过、不回退旧通道）；
- 409 device_bound：弹确认框，用户显式同意才 replace_device=true 重试
  （重新完成短信验证由服务端语义保证），不自动抢占；
- 登录成功回填会话，主窗状态行由调用方刷新。
"""
import threading
import tkinter as tk
from tkinter import ttk, messagebox

from toolbox import device_identity as di
from toolbox.account import (AccountClient, AccountError, ServiceNotReady)


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
        try:
            cli.ensure_pc_auth()
            say("服务就绪，请输入手机号")
            set_enabled(True)
            phone_e.focus_set()
        except ServiceNotReady as e:
            say(f"账号服务待就绪（{e}），稍后再试", warn=True)
            set_enabled(False)
        except AccountError as e:
            say(f"服务不可达：{e}", warn=True)
            set_enabled(False)

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

    def do_sms():
        phone = phone_var.get().strip()
        if not phone.isdigit() or len(phone) != 11:
            say("手机号不对呀（11 位数字）", warn=True)
            return

        def run():
            say("发送验证码…")
            try:
                r = cli.request_sms(phone)
                result["challenge"] = r.get("challenge_id")
                win.after(0, lambda: say(
                    "验证码已发送，查收短信" if result["challenge"]
                    else "服务未返回验证凭据（challenge_id），稍后再试",
                    warn=not result["challenge"]))
            except AccountError as e:
                win.after(0, lambda: say(f"发送失败：{e}", warn=True))
        _bg(run)

    def do_login():
        phone = phone_var.get().strip()
        code = code_var.get().strip()
        if not result.get("challenge"):
            say("先点「获取验证码」拿到短信凭据再登录", warn=True)
            return

        def run(replace=False):
            say("登录中…" + ("（换机确认）" if replace else ""))
            try:
                sess = cli.login(result["challenge"], code, phone=phone,
                                 replace_device=replace)
                result["sess"] = sess
                win.after(0, win.destroy)
            except AccountError as e:
                if e.code == "device_bound":
                    bound = (e.data.get("bound_device") or {})
                    msg = ("该账号已绑定其他电脑（%s）。\n换绑会使旧电脑登录失效，"
                           "确定换绑到本机吗？" % bound.get("digest", "未知设备"))
                    if messagebox.askyesno("换机确认", msg, parent=win):
                        run(replace=True)
                    else:
                        win.after(0, lambda: say("已取消换绑"))
                    return
                win.after(0, lambda: say(f"登录失败：{e}", warn=True))
        _bg(lambda: run(False))

    sms_btn.config(command=do_sms)
    login_btn.config(command=do_login)
    win.protocol("WM_DELETE_WINDOW", win.destroy)
    win.wait_visibility()
    win.grab_set()
    win.after(50, check)
    master.wait_window(win)
    return result["sess"]
