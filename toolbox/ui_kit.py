"""界面组件规范(REQ-20261022-007):间距基线 / 卡片 / 按钮 / 图标 / 空态。

设计原则(用户 UI 偏好:紧凑无装饰,不采立绘头像):
- 8px 间距基线(S1=4, S2=8, S3=12, S4=16),所有边距从这套值派生;
- 三档字体层级(标题 10/正文 9/辅助 8),不靠极浅颜色区分层级;
- 按钮按语义分档:主操作(紫)、CTA(粉)、次级(浅紫)、危险(描边红)、
  链接(纯文字);状态标签绝不带按钮外观;
- 图标用 Canvas 矢量绘制(不引入 emoji 或第三方图标库),与文字并存,
  并给控件保留可访问名称;
- 所有像素值经 dpi.px() 换算,保证 100~200% 缩放下清晰且不裁切。
"""
import tkinter as tk
from tkinter import ttk

from toolbox import dpi

FONT = "Microsoft YaHei UI"

# 间距基线(逻辑 px)
S1, S2, S3, S4 = 4, 8, 12, 16
CARD_PAD = S3
CARD_GAP = S2 + 2          # 10
ROW_H = 26

BRAND = {
    "page": "#F2F1F6", "card": "#FFFFFF", "side": "#FFFFFF",
    "primary": "#7C5CE0", "deep": "#3B2A5E", "cta": "#F4517B",
    "lav": "#EFE9FB", "line": "#E7E4F0",
    "hover": "#E3D9F7", "danger": "#D9534F", "danger_bg": "#FDECEA",
    "good": "#4CA37E", "warn": "#D99A3C", "dim": "#6F6577",
    "faint": "#8C8494", "fg": "#3A3238",
    # 兼容旧调用点(bad=C["bad"] / accent=C["accent"])
    "bad": "#D9534F", "accent": "#7C5CE0",
}

# 字体层级(点;Tk 会按 tk scaling 换算为物理像素)
F_H1 = (FONT, 11, "bold")
F_TITLE = (FONT, 10, "bold")
F_BODY = (FONT, 9)
F_SMALL = (FONT, 8)
F_DIALOG = (FONT, 12, "bold")


def font(size, bold=False):
    return (FONT, size, "bold") if bold else (FONT, size)


def setup_style(root):
    """统一 ttk 组件主题(clam 基底,扁平品牌化)。幂等。"""
    st = ttk.Style(root)
    try:
        st.theme_use("clam")
    except tk.TclError:
        pass
    pad = (dpi.px(10), dpi.px(5))
    st.configure("Brand.TButton", font=font(9), padding=pad,
                 background=BRAND["lav"], foreground=BRAND["deep"],
                 borderwidth=0, relief="flat", focusthickness=1,
                 focuscolor=BRAND["primary"])
    st.map("Brand.TButton",
           background=[("disabled", "#F5F3FA"), ("active", BRAND["hover"])],
           foreground=[("disabled", "#B8B2C6")])
    st.configure("Cta.TButton", font=font(9, True), padding=pad,
                 background=BRAND["cta"], foreground="#FFFFFF",
                 borderwidth=0, relief="flat")
    st.map("Cta.TButton",
           background=[("disabled", "#F3C7D3"), ("active", "#E23F69")],
           foreground=[("disabled", "#FFFFFF")])
    st.configure("Danger.TButton", font=font(9), padding=pad,
                 background=BRAND["card"], foreground=BRAND["danger"],
                 borderwidth=1, relief="solid")
    st.map("Danger.TButton",
           background=[("active", BRAND["danger_bg"])],
           foreground=[("disabled", "#C9A5A3")])
    st.configure("Ghost.TButton", font=font(8), padding=(dpi.px(6), dpi.px(2)),
                 background=BRAND["card"], foreground=BRAND["primary"],
                 borderwidth=0, relief="flat")
    st.map("Ghost.TButton", background=[("active", BRAND["lav"])])
    st.configure("Brand.TCheckbutton", font=font(9),
                 background=BRAND["card"], foreground=BRAND["deep"])
    st.map("Brand.TCheckbutton",
           background=[("active", BRAND["card"])])
    st.configure("Small.TButton", font=font(8),
                 padding=(dpi.px(8), dpi.px(3)),
                 background=BRAND["lav"], foreground=BRAND["deep"],
                 borderwidth=0, relief="flat")
    st.map("Small.TButton", background=[("active", BRAND["hover"])])
    return st


def button(parent, text, command=None, kind="brand", width=None):
    """语义按钮:brand / cta / danger / ghost / small。"""
    style = {"brand": "Brand.TButton", "cta": "Cta.TButton",
             "danger": "Danger.TButton", "ghost": "Ghost.TButton",
             "small": "Small.TButton"}[kind]
    kwargs = {"text": text, "style": style, "takefocus": True}
    if command is not None:
        kwargs["command"] = command
    if width:
        kwargs["width"] = width
    return ttk.Button(parent, **kwargs)


def card(master, title, padx=CARD_GAP, pady=(0, CARD_GAP)):
    """白卡片:标题行 + 内容区。返回内容 Frame(调用方往里 pack)。"""
    outer = tk.Frame(master, bg=BRAND["card"],
                     highlightbackground=BRAND["line"], highlightthickness=1)
    outer.pack(fill="x", padx=dpi.px(padx), pady=(dpi.px(pady[0]),
                                                 dpi.px(pady[1])))
    head = tk.Frame(outer, bg=BRAND["card"])
    head.pack(fill="x", padx=dpi.px(CARD_PAD), pady=(dpi.px(S2 + 2), 0))
    tk.Label(head, text=title, bg=BRAND["card"], fg=BRAND["deep"],
             font=F_TITLE, anchor="w").pack(side="left")
    body = tk.Frame(outer, bg=BRAND["card"])
    body.pack(fill="x", padx=dpi.px(CARD_PAD),
              pady=(dpi.px(S1), dpi.px(CARD_PAD - 2)))
    return body


def hint(master, text, wraplength=420, fg=None):
    """卡片外的全局说明(左边界与卡片内容对齐)。"""
    w = tk.Label(master, text=text, bg=BRAND["page"], fg=fg or BRAND["dim"],
                 font=F_SMALL, anchor="w", justify="left",
                 wraplength=dpi.px(wraplength))
    w.pack(fill="x", padx=dpi.px(CARD_GAP + 2), pady=(0, dpi.px(S2)))
    return w


# ---------------------------------------------------------------- 矢量图标

def _icon_canvas(parent, bg, size=16):
    s = dpi.px(size)
    return tk.Canvas(parent, width=s, height=s, bg=bg, bd=0,
                     highlightthickness=0, takefocus=0)


def draw_icon(canvas, kind, color, size=16):
    """在 Canvas 上重绘图标(导航选中态变色时调用)。"""
    canvas.delete("all")
    u = dpi.px(size) / 16.0
    lw = max(1, int(round(1.4 * u)))
    if kind == "home":
        canvas.create_line(2.5 * u, 7.6 * u, 8 * u, 2.6 * u, 13.5 * u,
                           7.6 * u, fill=color, width=lw, capstyle="round",
                           joinstyle="round")
        canvas.create_line(4.6 * u, 7.4 * u, 4.6 * u, 13.4 * u, 11.4 * u,
                           13.4 * u, 11.4 * u, 7.4 * u, fill=color, width=lw,
                           joinstyle="round")
    elif kind == "works":
        canvas.create_rectangle(2.4 * u, 3.4 * u, 13.6 * u, 12.6 * u,
                                outline=color, width=lw)
        canvas.create_polygon(6.8 * u, 6.1 * u, 6.8 * u, 9.9 * u,
                              10.6 * u, 8 * u, fill=color, outline=color)
    elif kind == "settings":
        canvas.create_oval(5.3 * u, 5.3 * u, 10.7 * u, 10.7 * u,
                           outline=color, width=lw)
        canvas.create_oval(7.2 * u, 7.2 * u, 8.8 * u, 8.8 * u,
                           fill=color, outline=color)
        for x1, y1, x2, y2 in ((8, 1.8, 8, 3.8), (8, 12.2, 8, 14.2),
                               (1.8, 8, 3.8, 8), (12.2, 8, 14.2, 8)):
            canvas.create_line(x1 * u, y1 * u, x2 * u, y2 * u, fill=color,
                               width=lw, capstyle="round")
    elif kind == "folder":
        canvas.create_line(2.2 * u, 4.4 * u, 6.6 * u, 4.4 * u, 7.8 * u,
                           6 * u, 13.8 * u, 6 * u, 13.8 * u, 12.4 * u,
                           2.2 * u, 12.4 * u, 2.2 * u, 4.4 * u,
                           fill=color, width=lw, joinstyle="round")
    elif kind == "empty":
        canvas.create_oval(3 * u, 3 * u, 13 * u, 13 * u, outline=color,
                           width=lw)
        canvas.create_line(8 * u, 5.6 * u, 8 * u, 8.4 * u, fill=color,
                           width=lw, capstyle="round")
        canvas.create_oval(7.4 * u, 10.2 * u, 8.6 * u, 11.4 * u,
                           outline=color, fill=color)


def icon(parent, kind, color, bg, size=16):
    c = _icon_canvas(parent, bg, size)
    draw_icon(c, kind, color, size)
    return c


# ---------------------------------------------------------------- 滚动容器

class ScrollFrame(tk.Frame):
    """竖向滚动容器:内容超出时出现滚动条,滚轮可用。

    用于主窗内容区与任务队列——小屏 / 高缩放下不裁切(REQ-006 D03)。
    """

    def __init__(self, master, bg, height=None, width=None):
        super().__init__(master, bg=bg)
        self.canvas = tk.Canvas(self, bg=bg, bd=0, highlightthickness=0,
                                takefocus=0)
        self.vbar = ttk.Scrollbar(self, orient="vertical",
                                  command=self.canvas.yview)
        self.canvas.configure(yscrollcommand=self.vbar.set)
        self.vbar.pack(side="right", fill="y")
        self.canvas.pack(side="left", fill="both", expand=True)
        if height:
            self.canvas.configure(height=dpi.px(height))
        if width:
            self.canvas.configure(width=dpi.px(width))
        self.inner = tk.Frame(self.canvas, bg=bg)
        self._win = self.canvas.create_window((0, 0), window=self.inner,
                                              anchor="nw")
        self.inner.bind("<Configure>", self._on_inner)
        self.canvas.bind("<Configure>", self._on_canvas)
        for seq in ("<Enter>", "<MouseWheel>"):
            self.canvas.bind_all(seq, self._wheel, add="+")

    def _on_inner(self, _e=None):
        self.canvas.configure(scrollregion=self.canvas.bbox("all"))
        self._autohide()

    def _on_canvas(self, e):
        self.canvas.itemconfigure(self._win, width=e.width)
        self._autohide()

    def _autohide(self):
        """内容不超高时收起滚动条(避免满屏无意义的滚动槽)。"""
        try:
            need = self.inner.winfo_reqheight() > self.canvas.winfo_height()
        except tk.TclError:
            return
        if need and not self.vbar.winfo_ismapped():
            self.vbar.pack(side="right", fill="y")
        elif not need and self.vbar.winfo_ismapped():
            self.vbar.pack_forget()

    def _wheel(self, e):
        """只在指针位于本容器范围内时滚动(多容器共用一个全局绑定)。"""
        try:
            x, y = self.canvas.winfo_rootx(), self.canvas.winfo_rooty()
            w, h = self.canvas.winfo_width(), self.canvas.winfo_height()
            if not (x <= e.x_root <= x + w and y <= e.y_root <= y + h):
                return
        except (tk.TclError, AttributeError):
            return
        delta = int(-1 * (e.delta / 120)) if getattr(e, "delta", 0) else 0
        if delta:
            self.canvas.yview_scroll(delta, "units")

    def scroll_top(self):
        self.canvas.yview_moveto(0)


def checkbutton(parent, text, variable, command=None, bg=None):
    """品牌化复选框(tk 原生,保证选中色与主题一致)。"""
    bg = bg or BRAND["card"]
    return tk.Checkbutton(
        parent, text=text, variable=variable, command=command, bg=bg,
        fg=BRAND["deep"], activebackground=bg, activeforeground=BRAND["deep"],
        selectcolor="#FFFFFF", font=F_BODY, bd=0, highlightthickness=1,
        highlightbackground=bg, highlightcolor=BRAND["primary"],
        anchor="e", cursor="hand2", padx=dpi.px(2))


def empty_state(master, title, detail, bg=None):
    """空态:图标 + 标题 + 原因/下一步(REQ-007 V14)。"""
    bg = bg or BRAND["card"]
    box = tk.Frame(master, bg=bg)
    ic = icon(box, "empty", BRAND["faint"], bg, size=22)
    ic.pack(pady=(dpi.px(S2), dpi.px(S1)))
    tk.Label(box, text=title, bg=bg, fg=BRAND["deep"], font=font(9, True)).pack()
    tk.Label(box, text=detail, bg=bg, fg=BRAND["dim"], font=F_SMALL,
             wraplength=dpi.px(360), justify="center").pack(
        pady=(dpi.px(2), dpi.px(S2)))
    return box
