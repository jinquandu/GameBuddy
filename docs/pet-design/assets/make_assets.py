# -*- coding: utf-8 -*-
"""桌宠素材加工：人设图裁切件 → toolbox/pet_assets/ 透明 PNG。

用法（仓库根目录）：py docs/pet-design/assets/make_assets.py

处理：
1. 抠背景：从四边洪泛去除米白底（容差 34，边缘二次羽化）；
2. 派生：眨眼帧（整图纵向 94% 压扁，顶对齐）、Q版迷你尺寸；
3. 头像：face_a 头部圆形裁剪 + 珊瑚粉描边；
4. 圆角蒙版：窗口四角 20px（透明区为 MAGIC 品红，配合 -transparentcolor）。

仅依赖 Pillow；重跑覆盖旧产物。
"""
from PIL import Image, ImageDraw, ImageFilter
from pathlib import Path
import collections

SRC = Path(__file__).resolve().parent              # docs/pet-design/assets/
OUT = SRC.parent.parent.parent / "toolbox" / "pet_assets"
MAGIC = (255, 0, 255)                              # -transparentcolor 魔法色
TOL, TOL2 = 34, 52                                 # 洪容差 / 羽化容差


def _bg_of(im):
    px = im.load()
    pts = [px[2, 2], px[im.width - 3, 2], px[2, im.height - 3],
           px[im.width - 3, im.height - 3], px[im.width // 2, 1]]
    return tuple(sum(c[i] for c in pts) // len(pts) for i in range(3))


def _near(c, bg, tol):
    return sum((a - b) * (a - b) for a, b in zip(c[:3], bg)) <= tol * tol


def unbg(im):
    """从边界洪泛去底 -> RGBA；边缘容差内像素半透明羽化。"""
    im = im.convert("RGB")
    bg = _bg_of(im)
    px = im.load()
    w, h = im.size
    seen = bytearray(w * h)
    dq = collections.deque()
    for x in range(w):
        dq += [(x, 0), (x, h - 1)]
    for y in range(h):
        dq += [(0, y), (w - 1, y)]
    while dq:
        x, y = dq.popleft()
        if x < 0 or y < 0 or x >= w or y >= h or seen[y * w + x]:
            continue
        seen[y * w + x] = 1
        if not _near(px[x, y], bg, TOL):
            continue
        dq += [(x - 1, y), (x + 1, y), (x, y - 1), (x, y + 1)]
    out = im.convert("RGBA")
    op = out.load()
    for y in range(h):                             # 边缘二次羽化
        for x in range(w):
            if seen[y * w + x]:
                op[x, y] = (*op[x, y][:3], 0)
            elif _near(op[x, y], bg, TOL2) and any(
                    0 <= x + dx < w and 0 <= y + dy < h and
                    seen[(y + dy) * w + x + dx]
                    for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1))):
                op[x, y] = (*op[x, y][:3], 110)
    return out, seen, w, h


def trim_alpha(im, bottom_cut=0):
    """按 alpha 包围盒裁掉透明边距；bottom_cut 再裁掉底部（原稿阴影带）。"""
    im = im.crop(im.getbbox() or (0, 0, im.width, im.height))
    if bottom_cut:
        im = im.crop((0, 0, im.width,
                      max(1, round(im.height * (1 - bottom_cut)))))
    return im


def fit_height(im, h):
    w = max(1, round(im.width * h / im.height))
    return im.resize((w, h), Image.LANCZOS)


def squash(im, ratio=0.94):
    """顶对齐纵向压扁（眨眼/俏皮感帧），保持画布原尺寸。"""
    nh = round(im.height * ratio)
    s = im.resize((im.width, nh), Image.LANCZOS)
    pad = Image.new("RGBA", im.size, (0, 0, 0, 0))
    pad.paste(s, (0, 0), s)
    return pad


def circle_avatar(im, size=64, ring=(240, 138, 133, 255), rw=4):
    """头部圆形头像 + 珊瑚粉描边。im 应为已抠透明 face 差分。"""
    head = im.crop((0, 0, im.width, round(im.height * 0.62)))
    side = min(head.size)
    head = head.crop((head.width // 2 - side // 4, 0,
                      head.width // 2 - side // 4 + side, side))
    head = head.resize((size - 2 * rw, size - 2 * rw), Image.LANCZOS)
    av = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(av)
    d.ellipse([0, 0, size - 1, size - 1], fill=ring)
    mask = Image.new("L", (size - 2 * rw,) * 2, 0)
    ImageDraw.Draw(mask).ellipse([0, 0, size - 2 * rw - 1, size - 2 * rw - 1],
                                 fill=255)
    av.paste(head, (rw, rw), mask)
    return av


def corner(size=20, radius=16, fill=(255, 254, 252, 255)):
    """左上角圆角蒙版：弧外 MAGIC（透明化），弧内窗口底色。"""
    im = Image.new("RGBA", (size, size), MAGIC + (255,))
    d = ImageDraw.Draw(im)
    d.pieslice([0, 0, 2 * radius, 2 * radius], 180, 270, fill=fill)
    d.rectangle([radius, 0, size, size], fill=fill)
    d.rectangle([0, radius, size, size], fill=fill)
    return im


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    srcs = {n: Image.open(SRC / f"{n}.png") for n in
            ("q_idle", "face_a", "face_b", "face_c")}
    clean = {n: unbg(im)[0] for n, im in srcs.items()}
    q = trim_alpha(clean["q_idle"], bottom_cut=0.08)   # 去原稿底部阴影带
    faces = {n: trim_alpha(clean[n]) for n in
             ("face_a", "face_b", "face_c")}

    fit_height(q, 168).save(OUT / "q_idle.png")
    squash(fit_height(q, 168)).save(OUT / "q_blink.png")
    fit_height(q, 116).save(OUT / "q_mini.png")
    for n in ("face_a", "face_b", "face_c"):
        fit_height(faces[n], 148).save(OUT / f"{n}.png")
    circle_avatar(faces["face_a"], 64, rw=4).save(OUT / "avatar.png")

    c = corner()
    c.save(OUT / "corner_tl.png")
    c.transpose(Image.FLIP_LEFT_RIGHT).save(OUT / "corner_tr.png")
    c.transpose(Image.FLIP_TOP_BOTTOM).save(OUT / "corner_bl.png")
    c.transpose(Image.ROTATE_180).save(OUT / "corner_br.png")

    for f in sorted(OUT.glob("*.png")):
        print(f"{f.name:16s} {f.stat().st_size // 1024}KB")


if __name__ == "__main__":
    main()
