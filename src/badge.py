"""
badge.py — текст -> бейдж (как UPDATE! на скрине).

render_static()  -> PNG/WEBP 512x512, прозрачный фон, скруглённый салатовый плашка + чёрный жирный текст.
render_wave_webm() -> WEBM (VP9, alpha), эффект развевающегося флага.

Эффект флага: по кадрам сдвигаем строки пикселей по синусоиде
  dy(x, t) = A * sin(k*x - w*t)
плюс лёгкий горизонтальный «провис» — получается живая ткань.
Кадры рендерим в PIL, склеиваем ffmpeg-ом в webm с прозрачностью (yuva420p).
"""

from __future__ import annotations
from io import BytesIO
from pathlib import Path
import os, math, shutil, subprocess, tempfile
import numpy as np
from PIL import Image, ImageDraw, ImageFont, ImageFilter

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

# палитра по скрину
GREEN = (181, 230, 53, 255)   # салатовый
INK = (17, 17, 17, 255)       # почти чёрный текст
PAD_X, PAD_Y = 46, 26         # внутренние отступы плашки
RADIUS = 34


def _best_font(text: str, max_w: int, max_h: int) -> ImageFont.FreeTypeFont:
    """Подбираем кегль так, чтобы текст влез в плашку."""
    size = 220
    while size > 12:
        f = _load_font(size)
        l, t, r, b = f.getbbox(text)
        if (r - l) <= max_w and (b - t) <= max_h:
            return f
        size -= 4
    return _load_font(12)


def _draw_badge(text: str, canvas: int = 512) -> Image.Image:
    """Рисуем плашку с текстом по центру прозрачного полотна canvas x canvas."""
    text = text.upper()
    # доступная область под плашку (с запасом под колыхание)
    max_text_w = canvas - 2 * PAD_X - 40
    max_text_h = canvas - 2 * PAD_Y - 120
    font = _best_font(text, max_text_w, max_text_h)

    l, t, r, b = font.getbbox(text)
    tw, th = r - l, b - t
    bw, bh = tw + 2 * PAD_X, th + 2 * PAD_Y

    img = Image.new("RGBA", (canvas, canvas), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    bx0 = (canvas - bw) // 2
    by0 = (canvas - bh) // 2

    # тень
    shadow = Image.new("RGBA", (canvas, canvas), (0, 0, 0, 0))
    ds = ImageDraw.Draw(shadow)
    ds.rounded_rectangle([bx0, by0 + 8, bx0 + bw, by0 + bh + 8], RADIUS, fill=(0, 0, 0, 90))
    shadow = shadow.filter(ImageFilter.GaussianBlur(7))
    img = Image.alpha_composite(img, shadow)
    d = ImageDraw.Draw(img)

    # плашка
    d.rounded_rectangle([bx0, by0, bx0 + bw, by0 + bh], RADIUS, fill=GREEN)
    # текст
    tx = bx0 + PAD_X - l
    ty = by0 + PAD_Y - t
    d.text((tx, ty), text, font=font, fill=INK)
    return img


def render_static(text: str, fmt: str = "WEBP") -> bytes:
    """512x512 статичный бейдж. fmt='WEBP' для стикера, 'PNG' для чего угодно."""
    img = _draw_badge(text, 512)
    buf = BytesIO()
    img.save(buf, format=fmt)
    return buf.getvalue()


def _wave_frame(base: np.ndarray, t: float, amp: float, k: float, w: float) -> np.ndarray:
    """Один кадр: вертикальный сдвиг каждого столбца по синусоиде (флаг)."""
    h, wd, _ = base.shape
    xs = np.arange(wd)
    # сдвиг по вертикали растёт слева->направо (как у флага на древке слева)
    shift = (amp * (xs / wd) * np.sin(k * xs - w * t)).astype(np.float32)
    out = np.zeros_like(base)
    for x in range(wd):
        dy = int(round(shift[x]))
        col = base[:, x, :]
        if dy > 0:
            out[dy:, x, :] = col[:h - dy]
        elif dy < 0:
            out[:h + dy, x, :] = col[-dy:]
        else:
            out[:, x, :] = col
    return out


def render_wave_webm(
    text: str,
    fps: int = 30,
    seconds: float = 2.0,
    amp: float = 16.0,
    waves: float = 2.2,
) -> bytes:
    """
    Развевающийся флаг -> webm (VP9 + alpha), 512x512.
    amp — амплитуда колыхания (px), waves — сколько волн по ширине.
    """
    if not shutil.which("ffmpeg"):
        raise RuntimeError("ffmpeg не найден")

    base_img = _draw_badge(text, 512)
    base = np.asarray(base_img)            # RGBA
    h, wd, _ = base.shape
    k = (2 * math.pi * waves) / wd
    n_frames = int(fps * seconds)
    w = 2 * math.pi / n_frames             # один полный цикл за ролик => бесшовный луп

    tmp = tempfile.mkdtemp(prefix="wave_")
    try:
        for i in range(n_frames):
            frame = _wave_frame(base, i, amp, k, w * fps)  # t=i, скорость задаём масштабом
            Image.fromarray(frame, "RGBA").save(os.path.join(tmp, f"f{i:03d}.png"))

        out = os.path.join(tmp, "out.webm")
        cmd = [
            "ffmpeg", "-y", "-framerate", str(fps),
            "-i", os.path.join(tmp, "f%03d.png"),
            "-c:v", "libvpx-vp9", "-pix_fmt", "yuva420p",
            "-auto-alt-ref", "0",                       # обязателен для альфы в vp9
            "-metadata:s:v:0", "alpha_mode=1",          # помечаем поток как имеющий альфу
            "-b:v", "0", "-crf", "32",
            "-an", out,
        ]
        subprocess.run(cmd, check=True, capture_output=True)
        with open(out, "rb") as f:
            return f.read()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
