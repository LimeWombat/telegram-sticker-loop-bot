"""
render_text.py — движок рендера текста для StickerLoopBot.

Пайплайн: текст → маска → заливка (цвет/градиент) → эффекты слоями
(мягкая тень / глоу / неон-каскад) → фон (прозрачный / solid / radial
с виньеткой) → опционально плашка (badge/card) под текстом.

Главные отличия от старой версии:
  - градиент заливает САМ ТЕКСТ (хром, золото, огонь), а не прямоугольник за ним;
  - тени мягкие (blur), не жёсткий офсет;
  - неон — трёхслойный: широкий ореол + плотное свечение + светлое ядро;
  - плашки — скруглённые карточки по размеру текста, не яйца на весь холст;
  - половина стилей на прозрачном фоне — в чате выглядит как стикер, не открытка.
"""
from __future__ import annotations

from dataclasses import dataclass
from io import BytesIO
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFont, ImageFilter

FONT_DIR = Path(__file__).resolve().parents[1] / "assets" / "fonts"
_FALLBACK_FONT = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"

# (файл вариативного шрифта, named instance)
FONTS = {
    "montserrat": (FONT_DIR / "montserrat.ttf", "ExtraBold"),
    "oswald":     (FONT_DIR / "oswald.ttf", "Bold"),
    "rubik":      (FONT_DIR / "rubik.ttf", "Black"),
    "playfair":   (FONT_DIR / "playfair.ttf", "Black"),
    "inter":      (FONT_DIR / "inter.ttf", "ExtraBold"),
    "unbounded":  (FONT_DIR / "unbounded.ttf", "Bold"),
}
DEFAULT_FONT = "montserrat"


def _font(spec: tuple, size: int) -> ImageFont.FreeTypeFont:
    path, instance = spec
    try:
        font = ImageFont.truetype(str(path), size)
    except OSError:
        return ImageFont.truetype(_FALLBACK_FONT, size)
    try:
        font.set_variation_by_name(instance)
    except Exception:
        pass
    return font


@dataclass(frozen=True)
class Style:
    key: str
    label: str          # для кнопок в боте
    bg: tuple           # ("none",) | ("solid",hex) | ("radial",inner,outer) — фон холста
    fill: tuple         # ("solid",hex) | ("grad",hex1,hex2) — заливка самого текста
    font: str
    effect: str         # "none" | "shadow" | "glow" | "neon"
    accent: str         # цвет эффекта (тень/свечение)
    plate: tuple | None = None   # (hex, alpha 0..255) — скруглённая плашка под текстом
    vignette: bool = False


STYLES: dict[str, Style] = {
    # текст-эффекты на тёмном фоне
    "chrome":  Style("chrome",  "🪩 Хром",     ("solid", "#101014"), ("grad", "#ffffff", "#7d838f"), "montserrat", "shadow", "#000000", vignette=True),
    "gold":    Style("gold",    "👑 Золото",   ("solid", "#0d0a05"), ("grad", "#f9e79b", "#b8860b"), "playfair", "glow", "#8a6b1f", vignette=True),
    "fire":    Style("fire",    "🔥 Огонь",    ("solid", "#140806"), ("grad", "#ffe259", "#ff3c00"), "montserrat", "glow", "#ff5a00", vignette=True),
    "frost":   Style("frost",   "🧊 Лёд",      ("solid", "#0a1220"), ("grad", "#e8f9ff", "#4aa8ff"), "inter", "glow", "#3d9bff", vignette=True),
    "neon":    Style("neon",    "💗 Неон",     ("radial", "#2a1045", "#0b0518"), ("solid", "#ffe9f6"), "inter", "neon", "#ff4fc3"),
    "acid":    Style("acid",    "🧪 Кислота",  ("solid", "#050505"), ("solid", "#c8ff00"), "unbounded", "glow", "#a4ff00"),
    "matrix":  Style("matrix",  "🟩 Матрица",  ("solid", "#020a04"), ("solid", "#26ff8a"), "oswald", "neon", "#00e561"),
    "violet":  Style("violet",  "🔮 Ультра",   ("radial", "#31135e", "#0e0420"), ("solid", "#f2eaff"), "unbounded", "neon", "#8a4dff"),
    "noir":    Style("noir",    "⚫ Нуар",     ("solid", "#0b0b0f"), ("solid", "#f4f4f6"), "montserrat", "shadow", "#000000", vignette=True),
    # плашки на прозрачном фоне — в чате выглядят как стикер
    "lime":    Style("lime",    "🟢 Лайм",     ("none",), ("solid", "#101208"), "rubik", "none", "#5f7d10", plate=("#c6f52e", 255)),
    "card":    Style("card",    "🤍 Карточка", ("none",), ("solid", "#101014"), "inter", "none", "#000000", plate=("#fafafa", 248)),
    "glass":   Style("glass",   "🫧 Стекло",   ("none",), ("solid", "#ffffff"), "inter", "shadow", "#000000", plate=("#ffffff", 68)),
}

DEFAULT_STYLE = "chrome"


# ── низкоуровневые помощники ───────────────────────────

def _hex(c: str, alpha: int = 255) -> tuple[int, int, int, int]:
    c = c.lstrip("#")
    return (int(c[0:2], 16), int(c[2:4], 16), int(c[4:6], 16), alpha)


def _linear_v(w: int, h: int, c1: str, c2: str) -> Image.Image:
    """Вертикальный градиент w×h (numpy, быстрый)."""
    a = np.array(_hex(c1), dtype=np.float32)
    b = np.array(_hex(c2), dtype=np.float32)
    t = np.linspace(0.0, 1.0, h, dtype=np.float32)[:, None, None]
    arr = a[None, None, :] * (1 - t) + b[None, None, :] * t
    return Image.fromarray(np.broadcast_to(arr, (h, w, 4)).astype(np.uint8), "RGBA")


def _radial(w: int, h: int, inner: str, outer: str) -> Image.Image:
    """Радиальный градиент: светлее в центре, темнее к краям."""
    a = np.array(_hex(inner), dtype=np.float32)
    b = np.array(_hex(outer), dtype=np.float32)
    ys, xs = np.mgrid[0:h, 0:w].astype(np.float32)
    cx, cy = (w - 1) / 2, (h - 1) / 2
    d = np.sqrt(((xs - cx) / (w / 2)) ** 2 + ((ys - cy) / (h / 2)) ** 2)
    t = np.clip(d / 1.25, 0, 1)[..., None]
    arr = a[None, None, :] * (1 - t) + b[None, None, :] * t
    return Image.fromarray(arr.astype(np.uint8), "RGBA")


def _apply_vignette(img: Image.Image, strength: float = 0.45) -> Image.Image:
    w, h = img.size
    ys, xs = np.mgrid[0:h, 0:w].astype(np.float32)
    cx, cy = (w - 1) / 2, (h - 1) / 2
    d = np.sqrt(((xs - cx) / (w / 2)) ** 2 + ((ys - cy) / (h / 2)) ** 2)
    dark = 1.0 - strength * np.clip(d - 0.55, 0, 1) ** 1.5
    arr = np.asarray(img.convert("RGBA")).astype(np.float32)
    arr[..., :3] *= dark[..., None]
    return Image.fromarray(np.clip(arr, 0, 255).astype(np.uint8), "RGBA")


def _colorized(mask: Image.Image, color: str, alpha_scale: float = 1.0) -> Image.Image:
    """RGBA-слой: цвет color с альфой из маски."""
    layer = Image.new("RGBA", mask.size, _hex(color))
    if alpha_scale >= 1.0:
        layer.putalpha(mask)
    else:
        layer.putalpha(mask.point(lambda v: int(v * alpha_scale)))
    return layer


# ── текст ──────────────────────────────────────────────

def _line_h(font: ImageFont.FreeTypeFont) -> int:
    return font.getbbox(" Адй")[3] + int(font.size * 0.32)


def _wrap(text: str, font: ImageFont.FreeTypeFont, max_w: int, draw: ImageDraw.ImageDraw) -> list[str]:
    words = text.split()
    if not words:
        return [text]
    lines, cur = [], words[0]
    for word in words[1:]:
        trial = cur + " " + word
        if draw.textlength(trial, font=font) <= max_w:
            cur = trial
        else:
            lines.append(cur)
            cur = word
    lines.append(cur)
    return lines


def _fit(text: str, font_spec: tuple, box_w: int, box_h: int, max_size: int,
         draw: ImageDraw.ImageDraw) -> tuple[ImageFont.FreeTypeFont, list[str]]:
    size = max_size
    while size >= 10:
        font = _font(font_spec, size)
        lines = _wrap(text, font, box_w, draw)
        widest = max(draw.textlength(ln, font=font) for ln in lines)
        lh = _line_h(font)
        total_h = lh * len(lines)
        if widest <= box_w and total_h <= box_h:
            return font, lines
        size -= 2
    font = _font(font_spec, 10)
    return font, _wrap(text, font, box_w, draw)


def _text_mask(size: tuple[int, int], lines: list[str], font: ImageFont.FreeTypeFont,
               area: tuple[int, int, int, int]) -> Image.Image:
    """L-маска текста, отцентрированного в area."""
    mask = Image.new("L", size, 0)
    draw = ImageDraw.Draw(mask)
    x0, y0, x1, y1 = area
    bw, bh = x1 - x0, y1 - y0
    lh = _line_h(font)
    total_h = lh * len(lines)
    cy = y0 + (bh - total_h) // 2
    for ln in lines:
        w = draw.textlength(ln, font=font)
        cx = x0 + (bw - w) // 2
        t = font.getbbox(ln)[1]
        draw.text((cx, cy - t), ln, font=font, fill=255)
        cy += lh
    return mask


def _text_bbox(mask: Image.Image) -> tuple[int, int, int, int] | None:
    return mask.getbbox()


def _fill_layer(mask: Image.Image, fill: tuple) -> Image.Image:
    """Заливка текста: solid или вертикальный градиент по маске."""
    w, h = mask.size
    if fill[0] == "grad":
        bbox = mask.getbbox() or (0, 0, w, h)
        grad_h = max(1, bbox[3] - bbox[1])
        grad = Image.new("RGBA", (w, h), (0, 0, 0, 0))
        grad.paste(_linear_v(w, grad_h, fill[1], fill[2]), (0, bbox[1]))
        grad.putalpha(mask)
        return grad
    return _colorized(mask, fill[1])


# ── эффекты ────────────────────────────────────────────

def _effect_layers(mask: Image.Image, effect: str, accent: str,
                   font_size: int) -> list[Image.Image]:
    """Слои ПОД заливкой текста (тени/свечения)."""
    layers: list[Image.Image] = []
    if effect == "shadow":
        blur = max(3, font_size // 14)
        off = max(2, font_size // 18)
        sh = mask.filter(ImageFilter.GaussianBlur(blur))
        shadow = _colorized(sh, accent, 0.55)
        shifted = Image.new("RGBA", mask.size, (0, 0, 0, 0))
        shifted.alpha_composite(shadow, (0, off))
        layers.append(shifted)
    elif effect == "glow":
        wide = mask.filter(ImageFilter.GaussianBlur(max(6, font_size // 5)))
        tight = mask.filter(ImageFilter.GaussianBlur(max(2, font_size // 16)))
        layers.append(_colorized(wide, accent, 0.75))
        layers.append(_colorized(tight, accent, 0.85))
    elif effect == "neon":
        halo = mask.filter(ImageFilter.GaussianBlur(max(10, font_size // 3)))
        wide = mask.filter(ImageFilter.GaussianBlur(max(5, font_size // 7)))
        tight = mask.filter(ImageFilter.GaussianBlur(max(2, font_size // 18)))
        layers.append(_colorized(halo, accent, 0.9))
        layers.append(_colorized(wide, accent, 0.9))
        layers.append(_colorized(tight, accent, 1.0))
    return layers


# ── плашка ─────────────────────────────────────────────

def _plate_layer(size: tuple[int, int], text_bbox: tuple[int, int, int, int],
                 plate: tuple, pad_x: int, pad_y: int) -> Image.Image:
    """Скруглённая карточка по размеру текста + мягкая тень под ней."""
    w, h = size
    x0, y0, x1, y1 = text_bbox
    bx0 = max(2, x0 - pad_x)
    by0 = max(2, y0 - pad_y)
    bx1 = min(w - 2, x1 + pad_x)
    by1 = min(h - 2, y1 + pad_y)
    radius = max(14, min(bx1 - bx0, by1 - by0) // 5)
    color, alpha = plate

    layer = Image.new("RGBA", size, (0, 0, 0, 0))
    # мягкая тень
    shadow = Image.new("L", size, 0)
    ImageDraw.Draw(shadow).rounded_rectangle(
        [bx0, by0 + 6, bx1, by1 + 6], radius, fill=110)
    shadow = shadow.filter(ImageFilter.GaussianBlur(10))
    layer.alpha_composite(_colorized(shadow, "#000000"))
    # сама плашка + тонкая светлая рамка
    plate_mask = Image.new("L", size, 0)
    ImageDraw.Draw(plate_mask).rounded_rectangle([bx0, by0, bx1, by1], radius, fill=alpha)
    plate_img = Image.new("RGBA", size, _hex(color))
    plate_img.putalpha(plate_mask)
    layer.alpha_composite(plate_img)
    border = Image.new("RGBA", size, (0, 0, 0, 0))
    ImageDraw.Draw(border).rounded_rectangle(
        [bx0, by0, bx1, by1], radius, outline=(255, 255, 255, 90), width=2)
    layer.alpha_composite(border)
    return layer


# ── главный рендер ─────────────────────────────────────

def render_text_image(text: str, style_key: str = DEFAULT_STYLE,
                      width: int = 512, height: int | None = None,
                      pad: int | None = None) -> Image.Image:
    """
    Рендерит ПОЛНЫЙ текст (без обрезки) в картинку width×height.
    Если height=None — высота подбирается под текст (для стикеров).
    """
    st = STYLES.get(style_key, STYLES[DEFAULT_STYLE])
    text = text.strip() or "?"
    if pad is None:
        pad = max(10, width // 10)

    fixed_h = height
    canvas_h = fixed_h if fixed_h else max(width // 4, 160)
    font_spec = FONTS.get(st.font, FONTS[DEFAULT_FONT])

    scratch = Image.new("RGBA", (width, canvas_h))
    sdraw = ImageDraw.Draw(scratch)
    box_w = width - 2 * pad
    box_h = (fixed_h - 2 * pad) if fixed_h else (canvas_h * 3)
    max_size = int((fixed_h or width) * 0.62)
    font, lines = _fit(text, font_spec, box_w, box_h, max_size, sdraw)

    if not fixed_h:
        lh = _line_h(font)
        height = lh * len(lines) + 2 * pad
    else:
        height = fixed_h
    height += height % 2  # VP9/yuv420 требует чётную высоту
    size = (width, height)

    # фон
    kind = st.bg[0]
    if kind == "solid":
        img = Image.new("RGBA", size, _hex(st.bg[1]))
    elif kind == "radial":
        img = _radial(width, height, st.bg[1], st.bg[2])
    else:
        img = Image.new("RGBA", size, (0, 0, 0, 0))
    if st.vignette:
        img = _apply_vignette(img)

    # маска текста
    area = (pad, pad, width - pad, height - pad)
    mask = _text_mask(size, lines, font, area)
    bbox = _text_bbox(mask) or area

    # плашка под текстом
    if st.plate:
        img.alpha_composite(_plate_layer(size, bbox, st.plate,
                                         pad_x=max(14, font.size // 2),
                                         pad_y=max(10, font.size // 3)))

    # эффекты под заливкой
    for layer in _effect_layers(mask, st.effect, st.accent, font.size):
        img.alpha_composite(layer)

    # сам текст
    img.alpha_composite(_fill_layer(mask, st.fill))
    return img


def to_png(img: Image.Image) -> bytes:
    buf = BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def to_webp(img: Image.Image) -> bytes:
    buf = BytesIO()
    img.save(buf, format="WEBP", quality=95, method=6)
    return buf.getvalue()
