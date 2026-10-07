"""
emoji.py — текст/символ -> анимированная custom_emoji 100x100 (webm VP9 alpha).

Custom emoji жёстко 100x100. Текста влезает мало (1-4 символа норм),
поэтому это для коротких меток/иконок. Эффект — лёгкая пульсация + волна.
"""

from __future__ import annotations
from io import BytesIO
from pathlib import Path
import os, math, shutil, subprocess, tempfile
import numpy as np
from PIL import Image, ImageDraw, ImageFont

_FONT_PATH = Path(__file__).resolve().parents[1] / "assets" / "fonts" / "montserrat.ttf"
_FALLBACK_FONT = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"


def _load_font(size: int) -> ImageFont.FreeTypeFont:
    try:
        font = ImageFont.truetype(str(_FONT_PATH), size)
    except OSError:
        return ImageFont.truetype(_FALLBACK_FONT, size)
    try:
        font.set_variation_by_name("ExtraBold")
    except Exception:
        pass
    return font
GREEN = (181, 230, 53, 255)
INK = (17, 17, 17, 255)
SIZE = 100


def _emoji_base(text: str) -> Image.Image:
    text = text.upper()[:4]
    img = Image.new("RGBA", (SIZE, SIZE), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    # подбор кегля
    size = 80
    while size > 8:
        f = _load_font(size)
        l, t, r, b = f.getbbox(text)
        if (r - l) <= SIZE - 14 and (b - t) <= SIZE - 14:
            break
        size -= 2
    f = _load_font(size)
    l, t, r, b = f.getbbox(text)
    bw, bh = (r - l) + 18, (b - t) + 14
    bx, by = (SIZE - bw) // 2, (SIZE - bh) // 2
    d.rounded_rectangle([bx, by, bx + bw, by + bh], 14, fill=GREEN)
    d.text((bx + 9 - l, by + 7 - t), text, font=f, fill=INK)
    return img


def render_static_emoji(text: str) -> bytes:
    buf = BytesIO()
    _emoji_base(text).save(buf, format="PNG")
    return buf.getvalue()


def render_animated_emoji(text: str, fps: int = 30, seconds: float = 1.6) -> bytes:
    """Пульсирующая эмодзи -> webm 100x100 VP9 alpha."""
    base = _emoji_base(text)
    n = int(fps * seconds)
    tmp = tempfile.mkdtemp(prefix="emo_")
    try:
        for i in range(n):
            t = i / n
            scale = 1.0 + 0.10 * math.sin(2 * math.pi * t)   # дыхание ±10%
            s = max(1, int(SIZE * scale))
            fr = base.resize((s, s), Image.LANCZOS)
            canvas = Image.new("RGBA", (SIZE, SIZE), (0, 0, 0, 0))
            canvas.alpha_composite(fr, ((SIZE - s) // 2, (SIZE - s) // 2))
            canvas.save(os.path.join(tmp, f"f{i:03d}.png"))
        out = os.path.join(tmp, "o.webm")
        subprocess.run([
            "ffmpeg", "-y", "-framerate", str(fps),
            "-i", os.path.join(tmp, "f%03d.png"),
            "-c:v", "libvpx-vp9", "-pix_fmt", "yuva420p",
            "-auto-alt-ref", "0", "-metadata:s:v:0", "alpha_mode=1",
            "-b:v", "0", "-crf", "30", "-an", out,
        ], check=True, capture_output=True)
        return open(out, "rb").read()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
