# -*- coding: utf-8 -*-
"""陪玩大师姐全流程介绍 PDF 生成脚本。

品牌配色（应用端 UI V1 紫粉语言）：主紫 #7C5CE0、深紫 #3B2A5E、
粉 CTA #F4517B、浅紫 #EFE9FB。截图来自 docs/pdf-shots/。
"""
import os
from pathlib import Path

from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.lib import colors
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import (
    SimpleDocTemplate, Paragraph, Spacer, Image, Table, TableStyle,
    PageBreak, KeepTogether)

ROOT = Path(__file__).resolve().parent.parent
SHOTS = ROOT / "docs" / "pdf-shots"
OUT = ROOT / "docs" / "陪玩大师姐-全流程介绍.pdf"

BRAND = {"primary": colors.HexColor("#7C5CE0"),
         "deep": colors.HexColor("#3B2A5E"),
         "cta": colors.HexColor("#F4517B"),
         "lav": colors.HexColor("#EFE9FB"),
         "line": colors.HexColor("#E7E4F0"),
         "dim": colors.HexColor("#6F6577"),
         "fg": colors.HexColor("#3A3238"),
         "card": colors.white}

pdfmetrics.registerFont(TTFont("YH", r"C:\Windows\Fonts\msyh.ttc"))
pdfmetrics.registerFont(TTFont("YHB", r"C:\Windows\Fonts\msyhbd.ttc"))
pdfmetrics.registerFont(TTFont("HEI", r"C:\Windows\Fonts\simhei.ttf"))


def S(name, **kw):
    base = {"fontName": "YH", "fontSize": 10.5, "leading": 16,
            "textColor": BRAND["fg"], "wordWrap": "CJK"}
    base.update(kw)
    return ParagraphStyle(name, **base)


st_title = S("title", fontName="YHB", fontSize=22, leading=30,
             textColor=BRAND["deep"], spaceAfter=6)
st_subtitle = S("subtitle", fontSize=11, textColor=BRAND["dim"],
                spaceAfter=18)
st_h1 = S("h1", fontName="YHB", fontSize=15, leading=22,
          textColor=BRAND["primary"], spaceBefore=18, spaceAfter=8)
st_h2 = S("h2", fontName="YHB", fontSize=12, leading=18,
          textColor=BRAND["deep"], spaceBefore=12, spaceAfter=6)
st_body = S("body")
st_step = S("step", fontName="YHB", fontSize=11, leading=17,
            textColor=BRAND["deep"], spaceBefore=8, spaceAfter=4)
st_desc = S("desc", fontSize=9.5, leading=15, textColor=BRAND["dim"])
st_cap = S("cap", fontSize=8.5, leading=13, textColor=BRAND["dim"],
           alignment=1, spaceBefore=4, spaceAfter=12)
st_note = S("note", fontSize=9, leading=14, textColor=BRAND["dim"])

PAGE_W, PAGE_H = A4
CONTENT_W = PAGE_W - 30 * mm


def fig(path, caption, max_w=CONTENT_W):
    """图片等比缩放 + 标题。"""
    p = Path(path)
    if not p.is_file():
        return Paragraph(f"[截图缺失: {p.name}]", st_cap)
    from PIL import Image as PILImage
    with PILImage.open(p) as im:
        w, h = im.size
    ratio = h / w
    disp_w = min(max_w, 170 * mm)
    disp_h = disp_w * ratio
    if disp_h > 130 * mm:
        disp_h = 130 * mm
        disp_w = disp_h / ratio
    img = Image(str(p), width=disp_w, height=disp_h)
    return KeepTogether([img, Paragraph(caption, st_cap)])


def step_num(n, title, desc):
    """步骤行:编号圆 + 标题 + 说明。"""
    num_bg = Table(
        [[Paragraph(f"<font color='white' size='11'><b>{n}</b></font>",
                    S("num", fontName="YHB", alignment=1, leading=14))]],
        colWidths=[8 * mm], rowHeights=[8 * mm])
    num_bg.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), BRAND["primary"]),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("ALIGN", (0, 0), (-1, -1), "CENTER"),
        ("ROUNDEDCORNERS", [4, 4, 4, 4]),
    ]))
    txt = Paragraph(
        f"<font name='YHB' size='11' color='#3B2A5E'>{title}</font><br/>"
        f"<font size='9.5' color='#6F6577'>{desc}</font>", st_body)
    row = Table([[num_bg, txt]], colWidths=[12 * mm, CONTENT_W - 12 * mm])
    row.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (0, 0), 0),
        ("TOPPADDING", (0, 0), (-1, -1), 2),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 8),
    ]))
    return row


def divider():
    t = Table([[""]], colWidths=[CONTENT_W], rowHeights=[0.8])
    t.setStyle(TableStyle([
        ("LINEBELOW", (0, 0), (-1, -1), 0.6, BRAND["line"]),
    ]))
    return t


story = []

# ============ 封面区（首页顶部，不做独立封面页） ============
story.append(Spacer(1, 12 * mm))
story.append(Paragraph("陪玩大师姐", st_title))
story.append(Paragraph("从游戏录像到抖音发布 —— 全流程使用指南", st_subtitle))
story.append(divider())
story.append(Spacer(1, 4 * mm))

story.append(Paragraph(
    "你打游戏，大师姐替你把精彩瞬间剪成抖音短视频。整个流程全自动，"
    "你只需要打完游戏后去作品页挑一条最好的、在抖音里点发布。", st_body))
story.append(Spacer(1, 6 * mm))

# 流程总览图（纯文字表格）
flow_data = [
    ["① 安装登录", "② 选录像文件夹", "③ 打游戏", "④ 自动剪辑", "⑤ 选片发布"]]
flow_tbl = Table([flow_data[0]], colWidths=[CONTENT_W / 5] * 5)
flow_tbl.setStyle(TableStyle([
    ("FONTNAME", (0, 0), (-1, -1), "YHB"),
    ("FONTSIZE", (0, 0), (-1, -1), 9),
    ("TEXTCOLOR", (0, 0), (-1, -1), BRAND["deep"]),
    ("BACKGROUND", (0, 0), (-1, -1), BRAND["lav"]),
    ("ALIGN", (0, 0), (-1, -1), "CENTER"),
    ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
    ("TOPPADDING", (0, 0), (-1, -1), 8),
    ("BOTTOMPADDING", (0, 0), (-1, -1), 8),
    ("ROUNDEDCORNERS", [6, 6, 6, 6]),
]))
story.append(flow_tbl)
story.append(Spacer(1, 8 * mm))

# ============ 第一部分：应用端 ============
story.append(Paragraph("一、应用端 —— 装好就不用管了", st_h1))
story.append(Paragraph(
    "下载 GameBuddy-v0.1.zip，解压后双击 GameBuddy.exe 即可运行（免安装），"
    "所有数据保存在解压目录的 data 文件夹里。", st_body))
story.append(Spacer(1, 4 * mm))

story.append(step_num(1, "登录",
                      "首次打开 → 账号设置 → 输入手机号获取短信验证码 → 登录。"
                      "登录后大师姐就知道哪个账号是你的。"))

story.append(step_num(2, "绑定微信",
                      "登录后在「账号设置 → 去绑定」用手机微信扫码。"
                      "成片好了微信会通知你（可选，不影响其他功能）。"))

story.append(step_num(3, "选录像文件夹",
                      "工作台 → 更换文件夹 → 选一个你放游戏录像的文件夹。"
                      "大师姐每 2 小时自动检查一次有没有新录像。"))

story.append(fig(SHOTS / "settings.png",
                 "▲ 账号设置页：登录 / 权益 / 微信 / 环境四项条件，"
                 "全部打勾就可以自动剪辑"))

story.append(step_num(4, "开启自动录制（可选）",
                      "工作台「桌面录制」卡片勾选「自动录制」，进游戏就录、"
                      "退出就停，不用手动开关。也可以用手动录屏工具，"
                      "只要录像放进文件夹就行。"))

story.append(step_num(5, "等大师姐干活",
                      "凌晨 4 点到中午 12 点之间，只要待处理录像超过 2GB，"
                      "大师姐就自动开始剪辑（CPU 或内存超过 90% 时会等一等）。"
                      "任务队列实时显示进度和预计时间。"))

story.append(fig(SHOTS / "home.png",
                 "▲ 工作台：任务队列 + 每个任务的当前阶段、进度百分比和预计耗时"))

story.append(PageBreak())

# ============ 第二部分：作品页 ============
story.append(Paragraph("二、作品页 —— 挑一条，发出去", st_h1))
story.append(Paragraph(
    "云端剪辑完成后，大师姐会提示你「成片完成」。打开作品页，"
    "每天最多 3 条候选成片，挑 1 条最好的去发布。", st_body))
story.append(Spacer(1, 4 * mm))

story.append(fig(SHOTS / "works.png",
                 "▲ 我的作品：点击「打开作品页」进入选片"))

story.append(Paragraph("第 1 步：预览和编辑", st_h2))
story.append(Paragraph(
    "作品页顶部有三个标签页，对应 3 条候选成片（多杀拼接 / 云端特效集锦）。"
    "点击切换查看，视频直接在线播放。右侧可以改标题（55 字以内）和话题。"
    "勾选你要发的那一条。", st_body))
story.append(Spacer(1, 4 * mm))
story.append(fig(SHOTS / "works-step1-preview.png",
                 "▲ 成片工作台：预览视频 / 编辑标题和话题 / 勾选要发的成片"))

story.append(Paragraph("第 2 步：确认投稿", st_h2))
story.append(Paragraph(
    "点击「下一步：确认投稿」后，页面会展示你刚编辑好的标题、话题和封面。"
    "确认无误后点击「去抖音发布」，会自动打开抖音的发布页面。", st_body))
story.append(Spacer(1, 4 * mm))
story.append(fig(SHOTS / "works-step2-confirm.png",
                 "▲ 确认投稿页：核对标题 / 话题 / 封面后点击「去抖音发布」"))

story.append(Paragraph("第 3 步：在抖音里完成发布", st_h2))
story.append(Paragraph(
    "打开抖音后，视频和文案已经准备好了（手机直接打开，电脑用扫一扫）。"
    "在抖音里核对账号和封面，点击发布即可。大师姐不会代你发布——"
    "最后一步永远是你在抖音里亲手确认。", st_body))
story.append(Spacer(1, 4 * mm))
story.append(fig(SHOTS / "works-step2-qr.png",
                 "▲ 无法直接打开时：用抖音「扫一扫」识别二维码"))

# ============ 常见问题 ============
story.append(PageBreak())
story.append(Paragraph("三、常见问题", st_h1))

faqs = [
    ("为什么还没出片？",
     "检查三个条件：① 待处理录像是否超过 2GB；② 当前是否在工作时段"
     "（凌晨 4 点～中午 12 点）；③ 登录和微信绑定是否正常。"
     "任务队列里能看到每个任务的等待原因。"),
    ("为什么只有 3 条候选？",
     "每天最多生成一组（3 条）候选成片，你从中挑 1 条发布。"
     "这是产品规则，不是限制。"),
    ("发布后别人能看到吗？",
     "发布是在抖音里完成的，你可以在抖音里设置公开 / 好友可见 / 仅自己。"
     "大师姐不会替你做这个决定。"),
    ("换电脑了怎么办？",
     "登录时会自动检测设备变化，需要重新短信验证。原来的队列和成片"
     "不受影响。"),
    ("电脑配置不够怎么办？",
     "登录后大师姐会自动做一次环境检测（配置 / 磁盘 / 视频工具 / 识别速度），"
     "在「账号设置 → 环境」能看到结论。不达标时会告诉你差在哪。"),
    ("不想录了怎么停？",
     "工作台「桌面录制」取消勾选「自动录制」即可。已有的录像会继续处理。"),
]
for q, a in faqs:
    story.append(KeepTogether([
        Paragraph(f"Q：{q}", S("faq_q", fontName="YHB", fontSize=10.5,
                              textColor=BRAND["deep"], spaceBefore=10)),
        Paragraph(f"A：{a}", S("faq_a", fontSize=9.5, leading=15,
                              textColor=BRAND["dim"], leftIndent=6)),
    ]))

story.append(Spacer(1, 10 * mm))
story.append(divider())
story.append(Paragraph(
    "陪玩大师姐 · 让每一份实力都有机会被看见", st_cap))

# ============ 生成 ============
doc = SimpleDocTemplate(
    str(OUT), pagesize=A4,
    leftMargin=15 * mm, rightMargin=15 * mm,
    topMargin=15 * mm, bottomMargin=15 * mm,
    title="陪玩大师姐全流程介绍", author="大师姐")
doc.build(story)
print("PDF written:", OUT, f"({OUT.stat().st_size / 1024:.0f} KB)")
