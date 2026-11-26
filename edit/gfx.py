"""
gfx.py — процедурная графика ролика: карточки, главы, плашки-выноски, субтитры.

Всё рисуется Pillow'ом из кода (никакой ручной работы в GUI, как требует бриф).
Каждый оверлей — объект с методом frame(t), который возвращает RGBA-кадр 1920x1080.
Анимации: пружинный overshoot, появление по буквам, роста линии, виньетка-подсветка.
"""
from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter

from cfglib import (
    BRAND_RED,
    H,
    RARITY,
    W,
    bottom_gradient,
    draw_text_tracked,
    font,
    radial_glow,
    text_size,
    tracked_width,
)

# ─────────────────────────────── easing ───────────────────────────────────
def clamp(x, lo=0.0, hi=1.0):
    return max(lo, min(hi, x))


def smooth(x):
    x = clamp(x)
    return x * x * (3 - 2 * x)


def ease_out_cubic(x):
    x = clamp(x)
    return 1 - (1 - x) ** 3


def ease_out_expo(x):
    x = clamp(x)
    return 1 if x >= 1 else 1 - 2 ** (-10 * x)


def ease_in_out(x):
    x = clamp(x)
    return 4 * x ** 3 if x < 0.5 else 1 - (-2 * x + 2) ** 3 / 2


def ease_out_back(x, s=1.70158):
    x = clamp(x) - 1
    return 1 + (s + 1) * x ** 3 + s * x ** 2


def prog(t, t0, t1):
    return clamp((t - t0) / max(1e-6, t1 - t0))


def rgba(color, a=255):
    r, g, b = color[:3]
    return (int(r), int(g), int(b), int(a))


def with_alpha(img: Image.Image, k: float) -> Image.Image:
    """Умножить альфу изображения (0..1)."""
    if k >= 0.999:
        return img
    a = img.getchannel("A").point(lambda v: int(v * clamp(k)))
    out = img.copy()
    out.putalpha(a)
    return out


def glow_layer(size, color, radius=18, box=None, strength=1.0):
    """Свечение вокруг прямоугольника."""
    layer = Image.new("RGBA", size, (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    b = box or (0, 0, size[0], size[1])
    d.rounded_rectangle(b, radius=radius, fill=rgba(color, int(150 * strength)))
    return layer.filter(ImageFilter.GaussianBlur(radius // 2))


# ─────────────────────────────── оверлеи ──────────────────────────────────
class Overlay:
    """Базовый оверлей: длительность + фабрика кадра."""

    def __init__(self, spec: dict):
        self.spec = spec
        self.dur = float(spec.get("dur", 2.0))

    def frame(self, t: float) -> Image.Image:  # pragma: no cover - переопределяется
        raise NotImplementedError

    def frames(self, fps: int = 60):
        n = max(1, int(round(self.dur * fps)))
        for i in range(n):
            yield self.frame(i / fps)


# ─────────────────────────────── большие карточки ─────────────────────────
class Card(Overlay):
    """Открывающая / финальная карточка: размытый кадр + брендинг HoweMine."""

    def __init__(self, spec: dict):
        super().__init__(spec)
        self.bg_path = Path(spec["bg"]) if spec.get("bg") else None
        self.bg = None
        if self.bg_path and self.bg_path.exists():
            self.bg = Image.open(self.bg_path).convert("RGB").resize((W + 80, H + 80), Image.LANCZOS)
        self.title = spec.get("title", "HoweMine")
        self.sub = spec.get("sub", "ОБЗОР СБОРКИ НА ПРОДАЖУ")
        self.foot = spec.get("foot", "")
        self.kindcard = spec.get("card", "open")

    def _background(self, t: float) -> Image.Image:
        p = clamp(t / max(1e-6, self.dur))
        if self.bg is None:
            base = Image.new("RGB", (W, H), (10, 11, 16))
        else:
            # очень медленный push-out: 1.5 % за всю карточку
            k = 1.0 + 0.015 * p
            cw, ch = int((W + 80) / k), int((H + 80) / k)
            x0, y0 = ((W + 80) - cw) / 2, ((H + 80) - ch) / 2
            base = self.bg.crop((int(x0), int(y0), int(x0) + cw, int(y0) + ch)).resize((W, H), Image.LANCZOS)
        img = base.convert("RGBA")
        # плотная подложка, чтобы текст читался на любом кадре
        veil = Image.new("RGBA", (W, H), (8, 9, 14, 176))
        img = Image.alpha_composite(img, veil)
        img = Image.alpha_composite(img, bottom_gradient((W, H), strength=150, start=0.25))
        # мягкая виньетка
        vg = Image.new("L", (W, H), 0)
        vd = ImageDraw.Draw(vg)
        vd.ellipse([-W * 0.35, -H * 0.5, W * 1.35, H * 1.5], fill=90)
        vg = vg.filter(ImageFilter.GaussianBlur(160))
        img = Image.composite(img, Image.new("RGBA", (W, H), (0, 0, 0, 255)), vg.point(lambda v: 255 - v // 2))
        return img

    def frame(self, t: float) -> Image.Image:
        img = self._background(t)
        k_card = 1 - prog(t, self.dur - 0.45, self.dur)      # общий выход карточки
        # ── заголовок: трекинг «съезжается», масштаб с мягким overshoot
        tp = ease_out_cubic(prog(t, 0.06, 0.75))
        title_size = 196 if self.kindcard == "open" else 150
        f = font(title_size, bold=True)
        probe = ImageDraw.Draw(Image.new("RGBA", (8, 8)))
        base_track = 6
        track = 34 - (34 - base_track) * tp
        tw = tracked_width(probe, self.title, f, track)
        maxw = 1520
        if tw > maxw:
            f = font(int(title_size * maxw / tw), bold=True)
            tw = tracked_width(probe, self.title, f, track)
        ty = int(H * 0.365) - int((1 - tp) * 14)

        layer = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        ld = ImageDraw.Draw(layer)
        # лёгкое свечение бренда за заголовком
        glow = radial_glow((int(tw * 1.35), int(title_size * 1.5)), BRAND_RED, power=2.2,
                           peak=int(40 * tp * k_card))
        layer.alpha_composite(glow, (int((W - tw * 1.35) / 2), int(ty - title_size * 0.25)))
        # тень
        draw_text_tracked(ld, ((W - tw) / 2 + 3, ty + 4), self.title, f, (0, 0, 0, int(120 * tp * k_card)), track)
        draw_text_tracked(ld, ((W - tw) / 2, ty), self.title, f, rgba(BRAND_RED, int(255 * tp * k_card)), track)
        # лёгкий блик, проходящий по заголовку
        sweep = prog(t, 0.55, 1.45)
        if 0 < sweep < 1:
            mask = Image.new("L", (W, H), 0)
            md = ImageDraw.Draw(mask)
            draw_text_tracked(md, ((W - tw) / 2, ty), self.title, f, 255, track)
            bx = int(-400 + (W + 800) * ease_in_out(sweep))
            grad = Image.new("L", (int(tw + 400), H), 0)
            gd = ImageDraw.Draw(grad)
            for i in range(int(tw + 400)):
                d = abs(i - (bx - (W - tw) / 2 + 200))
                val = max(0.0, 1.0 - d / 190.0) ** 2 * 120
                gd.line([(i, 0), (i, H)], fill=int(val))
            gm = Image.new("L", (W, H), 0)
            gm.paste(grad, (int((W - tw) / 2) - 200, 0))
            mask = Image.composite(gm, Image.new("L", (W, H), 0), mask)
            layer.paste(Image.new("RGBA", (W, H), (255, 255, 255, 255)), (0, 0), mask)
        img = Image.alpha_composite(img, layer)

        d = ImageDraw.Draw(img)
        # ── линия-разделитель
        lp = ease_out_expo(prog(t, 0.42, 1.0)) * k_card
        half = int(230 * lp)
        if half > 1:
            cy = ty + int(title_size * 1.13)
            d.rectangle([W / 2 - half, cy, W / 2 + half, cy + 2], fill=(255, 255, 255, int(190 * k_card)))

        # ── подзаголовок: буквы проявляются по очереди
        sub_f = font(40, bold=True)
        track_sub = 9
        sw = tracked_width(probe, self.sub, sub_f, track_sub)
        if sw > 1300:
            sub_f = font(int(40 * 1300 / sw), bold=True)
            sw = tracked_width(probe, self.sub, sub_f, track_sub)
        sx0 = (W - sw) / 2
        sy = ty + int(title_size * 1.28)
        n = max(1, len(self.sub))
        for i, ch in enumerate(self.sub):
            a = clamp((prog(t, 0.62, 1.5) * (n + 4)) - i)
            if a <= 0:
                continue
            x = sx0 + tracked_width(probe, self.sub[:i], sub_f, track_sub)
            d.text((x, sy), ch, font=sub_f, fill=(255, 255, 255, int(245 * a * k_card)))

        if self.foot:
            fp = ease_out_cubic(prog(t, 0.9, 1.6)) * k_card
            ff = font(32, bold=False)
            fw, _ = text_size(d, self.foot, ff)
            d.text(((W - fw) / 2, sy + 66), self.foot, font=ff, fill=(196, 201, 212, int(225 * fp)))
        return img


# ─────────────────────────────── кинетические главы ───────────────────────
class Chapter(Overlay):
    """Кинетический титр главы: номер, заголовок, линия; под текстом — затемнение."""

    def __init__(self, spec: dict):
        super().__init__(spec)
        self.title = spec.get("title", "")
        self.num = spec.get("num", "")
        self.sub = spec.get("sub", "")
        self.x = int(spec.get("x", 110))
        self.y = int(spec.get("y", 700))

    def frame(self, t: float) -> Image.Image:
        img = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        t_in = ease_out_expo(prog(t, 0.0, 0.40))
        t_out = prog(t, self.dur - 0.30, self.dur)
        fade = 1 - t_out

        title_f = font(100, bold=True)
        kick_f = font(28, bold=True)
        sub_f = font(33, bold=False)
        probe = ImageDraw.Draw(Image.new("RGBA", (8, 8)))
        tw = tracked_width(probe, self.title, title_f, 4)

        # затемнение под текстом: горизонтальный градиент, чтобы не было «плашки»
        if t_in > 0.02:
            band_w = int((tw + self.x + 340) * t_in)
            band = Image.new("RGBA", (W, H), (0, 0, 0, 0))
            bd = ImageDraw.Draw(band)
            for i in range(band_w):
                a = int(150 * (1 - i / max(1, band_w)) ** 1.4 * fade)
                bd.line([(i, self.y - 46), (i, self.y + 186)], fill=(0, 0, 0, a))
            band = band.filter(ImageFilter.GaussianBlur(10))
            img = Image.alpha_composite(img, band)
            # бренд-полоса слева
            bar_h = int(166 * t_in)
            d0 = ImageDraw.Draw(img)
            d0.rectangle([self.x - 26, self.y - 24, self.x - 20, self.y - 24 + bar_h],
                         fill=rgba(BRAND_RED, int(235 * fade)))

        layer = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        d = ImageDraw.Draw(layer)
        dx = int((1 - t_in) * 64)
        if self.num:
            draw_text_tracked(d, (self.x + dx, self.y - 62), self.num, kick_f,
                              rgba(BRAND_RED, int(240 * fade)), 7)
        # заголовок
        draw_text_tracked(d, (self.x + dx + 2, self.y + 2), self.title, title_f,
                          (0, 0, 0, int(110 * fade)), 4)
        draw_text_tracked(d, (self.x + dx, self.y - 6), self.title, title_f,
                          (255, 255, 255, int(255 * fade)), 4)
        # линия под заголовком
        lw = int((tw + 60) * ease_out_expo(prog(t, 0.14, 0.6)))
        if lw > 2:
            d.rectangle([self.x + dx, self.y + 122, self.x + dx + lw, self.y + 126],
                        fill=rgba(BRAND_RED, int(240 * fade)))
        if self.sub:
            sa = int(215 * ease_out_cubic(prog(t, 0.24, 0.62)) * fade)
            d.text((self.x + dx, self.y + 142), self.sub, font=sub_f, fill=(214, 219, 229, sa))
        return Image.alpha_composite(img, layer)


# ─────────────────────────────── плашки-выноски ───────────────────────────
class Callout(Overlay):
    """
    Карточка-выноска: редкость, название, характеристики, линия-указатель к предмету
    и мягкая виньетка-подсветка объекта в кадре.
    """

    def __init__(self, spec: dict):
        super().__init__(spec)
        self.kicker = spec.get("kicker", "")
        self.title = spec.get("title", "")
        self.lines = list(spec.get("lines", []))
        self.rarity = spec.get("rarity", "epic")
        self.color = RARITY.get(self.rarity, (255, 255, 255))
        self.x = int(spec.get("x", 120))
        self.y = int(spec.get("y", 300))
        self.px = spec.get("px")
        self.py = spec.get("py")
        self.anchor_rect = spec.get("rect")
        self.width = int(spec.get("width", 0))
        self.big = bool(spec.get("big", False))

        self.pad = 38
        self.kick_f = font(30 if not self.big else 33, bold=True)
        self.title_f = font(58 if not self.big else 78, bold=True)
        self.line_f = font(31 if not self.big else 36, bold=False)
        probe = ImageDraw.Draw(Image.new("RGBA", (8, 8)))
        self.title_w = tracked_width(probe, self.title, self.title_f, 1)
        self.kick_w = tracked_width(probe, self.kicker.upper(), self.kick_f, 5)
        self.lines_w = max([probe.textlength(ln, font=self.line_f) for ln in self.lines] or [0])
        self.w = int(self.width or max(self.title_w, self.kick_w, self.lines_w) + 2 * self.pad + 26)
        th = text_size(probe, self.title, self.title_f)[1]
        self.h = int(self.pad * 0.55 + (40 if self.kicker else 0) + th + 16
                     + len(self.lines) * (text_size(probe, "Ай", self.line_f)[1] + 12) + self.pad * 0.7)

    def _card(self, title_progress: float, line_progress: float, alpha: float) -> Image.Image:
        card = Image.new("RGBA", (self.w, self.h), (0, 0, 0, 0))
        d = ImageDraw.Draw(card)
        d.rounded_rectangle([0, 0, self.w - 1, self.h - 1], radius=20, fill=(11, 12, 18, 226))
        d.rounded_rectangle([0, 0, self.w - 1, self.h - 1], radius=20, outline=rgba(self.color, 120), width=2)
        d.rounded_rectangle([0, 0, 8, self.h - 1], radius=4, fill=rgba(self.color, 240))
        y = int(self.pad * 0.55)
        x0 = self.pad
        if self.kicker:
            draw_text_tracked(d, (x0, y), self.kicker.upper(), self.kick_f, rgba(self.color, 245), 5)
            y += 40
        # заголовок по буквам
        n = max(1, len(self.title))
        shown = title_progress * (n + 3)
        tx = x0
        for i, ch in enumerate(self.title):
            a = clamp(shown - i)
            if a <= 0:
                break
            d.text((tx, y), ch, font=self.title_f, fill=(255, 255, 255, int(255 * a)))
            tx += d.textlength(ch, font=self.title_f)
        y += text_size(d, self.title, self.title_f)[1] + 16
        for ln in self.lines:
            d.text((x0, y), ln, font=self.line_f,
                   fill=(200, 206, 218, int(235 * line_progress)))
            y += text_size(d, ln, self.line_f)[1] + 12
        return with_alpha(card, alpha)

    def frame(self, t: float) -> Image.Image:
        img = Image.new("RGBA", (W, H), (0, 0, 0, 0))

        # подсветка объекта
        if self.anchor_rect:
            x0, y0, x1, y1 = self.anchor_rect
            pad = 110
            x0, y0, x1, y1 = max(0, x0 - pad), max(0, y0 - pad), min(W, x1 + pad), min(H, y1 + pad)
            gw, gh = int(x1 - x0), int(y1 - y0)
            if gw > 8 and gh > 8:
                g = radial_glow((gw, gh), self.color, power=1.5,
                                peak=int(105 * smooth(prog(t, 0.02, 0.5))))
                img.alpha_composite(g, (int(x0), int(y0)))

        t_in = prog(t, 0.02, 0.42)
        t_out = prog(t, self.dur - 0.28, self.dur)
        alpha = clamp(t_in * 2.5) * (1 - t_out)
        if alpha <= 0.01:
            return img
        sc = 0.88 + 0.12 * ease_out_back(t_in, 1.3)
        card = self._card(ease_out_cubic(prog(t, 0.14, 0.66)),
                          ease_out_cubic(prog(t, 0.34, 0.9)), alpha)
        if abs(sc - 1.0) > 0.003:
            nw, nh = max(2, int(card.width * sc)), max(2, int(card.height * sc))
            card = card.resize((nw, nh), Image.BICUBIC)
        cx = int(min(self.x, W - card.width - 30))
        cy = int(min(self.y, H - card.height - 30))
        shift = int(22 * (1 - ease_out_cubic(t_in)))
        img.alpha_composite(card, (cx - shift, cy + shift // 3))

        # линия-указатель к предмету
        if self.px is not None and self.py is not None:
            lp = ease_out_expo(prog(t, 0.12, 0.52)) * (1 - t_out)
            if lp > 0.02:
                sx = cx if self.px < cx + card.width / 2 else cx + card.width
                sy = cy + card.height // 2
                ex = sx + (float(self.px) - sx) * lp
                ey = sy + (float(self.py) - sy) * lp
                layer = Image.new("RGBA", (W, H), (0, 0, 0, 0))
                d = ImageDraw.Draw(layer)
                d.line([(sx, sy), (ex, ey)], fill=rgba(self.color, int(210 * alpha)), width=3)
                r = 6
                d.ellipse([ex - r, ey - r, ex + r, ey + r], fill=rgba(self.color, int(245 * alpha)))
                halo = radial_glow((110, 110), self.color, power=1.5, peak=190)
                layer.alpha_composite(with_alpha(halo, alpha), (int(ex) - 55, int(ey) - 55))
                img = Image.alpha_composite(img, layer)
        return img


# ─────────────────────────────── субтитры / подписи ───────────────────────
class Caption(Overlay):
    """Нижняя подпись (ключевая реплика/факт) с мягкой плашкой."""

    def __init__(self, spec: dict):
        super().__init__(spec)
        self.text = spec.get("text", "")
        self.y = int(spec.get("y", H - 148))
        self.size = int(spec.get("size", 42))
        self.color = spec.get("color", "white")
        self.align = spec.get("align", "center")
        self.x = spec.get("x")

    def frame(self, t: float) -> Image.Image:
        img = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        f = font(self.size, bold=True)
        d = ImageDraw.Draw(img)
        a_in = ease_out_cubic(prog(t, 0.0, 0.22))
        a_out = 1 - prog(t, self.dur - 0.22, self.dur)
        a = a_in * a_out
        if a <= 0.01:
            return img
        tw, th = text_size(d, self.text, f)
        if self.x is not None:
            x = int(self.x)
        elif self.align == "left":
            x = 110
        else:
            x = int((W - tw) / 2)
        pad_x, pad_y = 34, 18
        box = [x - pad_x, self.y - pad_y, x + tw + pad_x, self.y + th + pad_y + 12]
        plate = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        pd = ImageDraw.Draw(plate)
        pd.rounded_rectangle(box, radius=14, fill=(8, 9, 13, int(190 * a)))
        plate = plate.filter(ImageFilter.GaussianBlur(1))
        img.alpha_composite(plate)
        color = (255, 255, 255, int(250 * a)) if self.color == "white" else rgba(BRAND_RED, int(250 * a))
        d = ImageDraw.Draw(img)
        d.text((x + 1, self.y + 1), self.text, font=f, fill=(0, 0, 0, int(120 * a)))
        d.text((x, self.y), self.text, font=f, fill=color)
        return img


class Stamp(Overlay):
    """Крупный штамп поверх кадра (freeze-frame подпись, «ЦЕНА» и пр.)."""

    def __init__(self, spec: dict):
        super().__init__(spec)
        self.text = spec.get("text", "")
        self.sub = spec.get("sub", "")
        self.color = spec.get("color", "brand")

    def frame(self, t: float) -> Image.Image:
        img = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        a = ease_out_back(prog(t, 0.0, 0.3))
        out = 1 - prog(t, self.dur - 0.22, self.dur)
        f = font(120, bold=True)
        d = ImageDraw.Draw(img)
        tw, th = text_size(d, self.text, f)
        col = rgba(BRAND_RED, int(255 * out)) if self.color == "brand" else (255, 255, 255, int(255 * out))
        cy = H * 0.42
        layer = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        ld = ImageDraw.Draw(layer)
        ld.text(((W - tw) / 2, cy - th / 2), self.text, font=f, fill=col)
        if self.sub:
            sf = font(38, bold=False)
            sw, _ = text_size(ld, self.sub, sf)
            ld.text(((W - sw) / 2, cy + th / 2 + 18), self.sub, font=sf,
                    fill=(235, 238, 245, int(230 * out)))
        k = 0.9 + 0.1 * clamp(a)
        nw, nh = int(W * k), int(H * k)
        layer = layer.resize((nw, nh), Image.BILINEAR).crop(((nw - W) // 2, (nh - H) // 2,
                                                             (nw - W) // 2 + W, (nh - H) // 2 + H))
        img.alpha_composite(with_alpha(layer, clamp(a)))
        return img


class Flash(Overlay):
    """Световой флеш (на ударе/взрыве) — короткий, не белый, а тёплый."""

    def frame(self, t: float) -> Image.Image:
        k = (1 - prog(t, 0.0, self.dur)) ** 2
        img = Image.new("RGBA", (W, H), (255, 246, 235, int(150 * k)))
        return img


class Bars(Overlay):
    """Кинематографические полосы для хука/финала."""

    def frame(self, t: float) -> Image.Image:
        h = int(90 * (1 - prog(t, self.dur - 0.3, self.dur)))
        img = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        d = ImageDraw.Draw(img)
        d.rectangle([0, 0, W, h], fill=(0, 0, 0, 255))
        d.rectangle([0, H - h, W, H], fill=(0, 0, 0, 255))
        return img


TYPES = {
    "open_card": Card,
    "end_card": Card,
    "chapter": Chapter,
    "callout": Callout,
    "caption": Caption,
    "stamp": Stamp,
    "flash": Flash,
    "bars": Bars,
}


def build(spec: dict) -> Overlay:
    kind = spec.get("type")
    cls = TYPES.get(kind)
    if cls is None:
        raise KeyError(f"неизвестный тип оверлея: {kind}")
    if kind == "open_card":
        spec = {**spec, "card": "open"}
    if kind == "end_card":
        spec = {**spec, "card": "end"}
    return cls(spec)
