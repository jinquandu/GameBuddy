"""微信绑定对话框（REQ-20261021-001 规则1/方案A；S1 端点 wechat-binding*）。

素朴紧凑（用户 UI 偏好）：一张二维码 + 六位配对码 + 一行状态 +
「重新生成 / 确认绑定」两键。流程：PC create 拿 scan_url → 手机微信
扫码核验（state=awaiting_pc）→ 本机 confirm 收口。轮询 3s；二维码 TTL
300s，过期/取消可重新生成（服务端自动作废旧 pending 配对）。绑定与通知
开关分离（方案A §4），本对话框不管通知开关（手机侧扫码页自带选择）。

错误路径铁律与 account_ui 相同（REQ-20261019-014 P0）：后台线程不碰 Tk
（一律 win.after 回主线程）；except 变量出块即清，禁止闭包引用——先转
安全文本再排回调；失败/超时必须明确落地并恢复按钮；手机号、票据、
token 不进任何日志（配对码仅显示，不落 log）。
"""
import threading
import time
import tkinter as tk
import webbrowser
from tkinter import ttk

from toolbox.account import AccountClient, AccountError
from toolbox.logs import get as _getlog

ALOG = _getlog("account")

try:                          # 画码仅此一处用；老安装底座缺库 → 降级为
    import qrcode             # 「浏览器打开绑定页」按钮，流程不变
    from PIL import ImageTk
except ImportError:
    qrcode = None

STATE_TEXT = {
    "pending": "等待扫码：用手机微信扫上方二维码",
    "scanned": "已扫码，微信核验中…",
    "verifying": "微信核验中…",
    "awaiting_pc": "微信已验证 ✓ 请点「确认绑定」收口",
    "confirmed": "绑定完成 ✓",
    "expired": "二维码已过期，请「重新生成」",
    "cancelled": "已取消，可「重新生成」",
}
_ACTIVE = ("pending", "scanned", "verifying", "awaiting_pc")  # 可取消态


def _err_text(stage, exc, t0):
    """异常 → 安全文本（出 except 块前转字符串），带脱敏诊断。"""
    dur = int((time.time() - t0) * 1000)
    if isinstance(exc, AccountError):
        head = f"{stage}失败：{exc}"
        code, status = exc.code, exc.status
    else:
        head = f"{stage}异常：{exc!r}"
        code, status = "unexpected", ""
    return head + f"（{code}" + (f" HTTP{status}" if status else "") \
        + f" · {dur}ms）"


def binding_dialog(master, api_url, production_url=None):
    """模态绑定；返回收口后现查的 {state: bound|none, ...} 或 {}（未完成）。"""
    win = tk.Toplevel(master)
    win.title("微信绑定")
    win.resizable(False, False)
    win.configure(bg="#F6F5F2")
    win.transient(master)
    f = ttk.Frame(win, padding=(14, 12))
    f.pack(fill="both", expand=True)

    st = tk.Label(f, text="检查服务能力…", bg="#F6F5F2", fg="#8A8A85",
                  font=("Microsoft YaHei UI", 9), wraplength=240,
                  justify="left")
    st.pack(fill="x")

    qr_lab = tk.Label(f, bg="#FFFFFF", width=26, height=12)
    qr_lab.pack(pady=(8, 2))
    code_lab = tk.Label(f, text=" ", bg="#F6F5F2", fg="#5A7A4F",
                        font=("Microsoft YaHei UI", 11))
    code_lab.pack()

    row = ttk.Frame(f)
    row.pack(fill="x", pady=(8, 0))
    regen_btn = ttk.Button(row, text="重新生成", width=10)
    regen_btn.pack(side="left", padx=(0, 6))
    ok_btn = ttk.Button(row, text="确认绑定", width=10)
    ok_btn.pack(side="left")
    if qrcode is None:
        ttk.Button(f, text="在浏览器打开绑定页（转发到手机微信）",
                   command=lambda: webbrowser.open(result["scan_url"])
                   ).pack(pady=(6, 0))

    cli = AccountClient(api_url, production_url)
    result = {"req": None, "state": None, "scan_url": "", "done": {}}
    busy = {"on": False}
    alive = {"yes": True}

    def say(text, warn=False):
        st.config(text=text, fg="#C0392B" if warn else "#5A7A4F")

    def _sync_buttons():
        """按钮态唯一出口：忙时全禁；闲时「确认绑定」仅 awaiting_pc 可点。"""
        on = not busy["on"] and alive["yes"]
        regen_btn.config(state="normal" if on else "disabled")
        ok_btn.config(state="normal" if on
                      and result["state"] == "awaiting_pc" else "disabled")

    def _main(fn):
        win.after(0, fn)                       # Tk 只在主线程动

    def _bg(fn):
        if busy["on"]:
            return
        busy["on"] = True
        _sync_buttons()

        def wrap():
            try:
                fn()
            finally:
                busy["on"] = False
                if alive["yes"]:
                    _main(_sync_buttons)
        threading.Thread(target=wrap, daemon=True).start()

    def _fail(stage, exc, t0):
        msg = _err_text(stage, exc, t0)
        ALOG.warning("binding %s失败", stage)
        _main(lambda m=msg: say(m, warn=True))

    def _show(row_):
        """create/lookup 响应 → 界面（仅主线程调用）。"""
        result["req"] = row_.get("request_id") or result["req"]
        state = row_.get("state", "")
        result["state"] = state
        url = row_.get("scan_url")
        if url:
            result["scan_url"] = url
            if qrcode is not None:
                photo = ImageTk.PhotoImage(
                    qrcode.make(url, box_size=6, border=2))
                qr_lab.config(image=photo, text="",
                              width=photo.width(), height=photo.height())
                qr_lab.image = photo            # 防 GC 回收
            else:
                qr_lab.config(image="", text="（缺二维码库：请点下方按钮\n"
                               "打开绑定页，转发到手机微信完成核验）",
                              fg="#B0B0AA")
        code = row_.get("pairing_code")
        if code:
            code_lab.config(text=f"配对码 {code}")
        say(STATE_TEXT.get(state, f"状态：{state or '未知'}"),
            warn=state in ("expired", "cancelled"))
        _sync_buttons()

    def _poll():
        """3s 轮询配对状态；HTTP 在后台线程，渲染回主线程。"""
        if not alive["yes"] or not result["req"]:
            return

        def run():
            t0 = time.time()
            try:
                r = cli.binding_lookup(result["req"])
                _main(lambda r=r: _on_polled(r))
            except AccountError as e:
                # 单次查询失败不停轮询（网络抖动），下一 tick 再试
                ALOG.warning("binding lookup 失败 code=%s status=%s 耗时%dms",
                             e.code, e.status, (time.time() - t0) * 1000)
                _main(lambda: alive["yes"] and win.after(3000, _poll))
        threading.Thread(target=run, daemon=True).start()

    def _on_polled(r):
        if not alive["yes"]:
            return
        if r.get("state") != result["state"]:
            _show(r)
        if r.get("state") != "confirmed":
            win.after(3000, _poll)

    def do_create():
        def run():
            t0 = time.time()
            _main(lambda: say("生成绑定二维码…"))
            try:
                cli.ensure_pc_auth()
                cli.ensure_binding_ready()
                r = cli.binding_request()
                ALOG.info("binding create ok 耗时%dms",
                          (time.time() - t0) * 1000)
                _main(lambda r=r: (_show(r), win.after(3000, _poll)))
            except AccountError as e:
                _fail("生成二维码", e, t0)
            except Exception as ex:              # noqa: BLE001
                _fail("生成二维码", ex, t0)
        _bg(lambda: run())

    def do_confirm():
        def run():
            t0 = time.time()
            _main(lambda: say("确认绑定…"))
            try:
                cli.binding_confirm(result["req"])
                final = cli.wechat_binding()     # 现查收口后状态
                result["done"] = final
                ALOG.info("binding confirm ok state=%s 耗时%dms",
                          final.get("state"), (time.time() - t0) * 1000)
                _main(win.destroy)
            except AccountError as e:
                _fail("确认绑定", e, t0)
                _main(lambda: _show({"state": result["state"] or "pending"}))
                _main(lambda: alive["yes"] and win.after(3000, _poll))
            except Exception as ex:              # noqa: BLE001
                _fail("确认绑定", ex, t0)
        _bg(lambda: run())

    def on_close():
        alive["yes"] = False
        rid, state = result["req"], result["state"]
        if rid and state in _ACTIVE:            # 关窗清理 pending 配对

            def cancel():
                try:
                    cli.binding_cancel(rid)
                except Exception:               # noqa: BLE001 静默：可重新生成
                    pass
            threading.Thread(target=cancel, daemon=True).start()
        win.destroy()

    def check():
        """打开即能力门控（pc_auth + 绑定契约）；未就绪如实提示不降级。"""
        def run():
            t0 = time.time()
            try:
                cli.ensure_pc_auth()
                cli.ensure_binding_ready()
                _main(do_create)
            except AccountError as e:
                msg = ("微信绑定服务暂未就绪" if e.code == "service_not_ready"
                       else f"服务不可达：{e}")
                msg += f"（{e.code}" + (f" HTTP{e.status}" if e.status else "") \
                    + f" · {int((time.time() - t0) * 1000)}ms），稍后再试"
                ALOG.warning("binding gate code=%s", e.code)
                _main(lambda m=msg: (say(m, warn=True),
                                     regen_btn.config(state="disabled")))
        threading.Thread(target=run, daemon=True).start()

    regen_btn.config(command=do_create)
    ok_btn.config(command=do_confirm)
    _sync_buttons()                             # 能力确认前按钮一律禁用
    win.protocol("WM_DELETE_WINDOW", on_close)
    win.wait_visibility()
    win.grab_set()
    win.after(50, check)
    master.wait_window(win)
    return result["done"]
