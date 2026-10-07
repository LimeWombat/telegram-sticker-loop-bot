"""
animate.py — анимирование готовой картинки текста в webm (VP9 alpha) для Telegram.

Эффекты (все loop-seamless, частоты кратны длине ролика):
  sheen   — двойной диагональный блик: яркое ядро + широкий мягкий ореол,
            высветление к белому (не пересвет), лёгкий ease на движении;
  wave    — флаг: два синусных гармоника по колонкам, живая ткань;
  pop     — «дыхание» масштаба с мягким вертикальным покачиванием;
  glow    — неон: размытый ореол из самой картинки позади + пульс яркости;
  rainbow — плавный прогон оттенка по кругу (hue-rotate), альфа не трогаем;
  shake   — упругая тряска: целочисленные частоты => бесшовный луп.

Все эффекты сохраняют альфу (у pill/none стилей прозрачный фон важен).
"""
from __future__ import annotations

import math
import os
import shutil
import subprocess
import tempfile

import numpy as np
from PIL import Image, ImageFilter


def _sheen_frame(base: Image.Image, t: float) -> Image.Image:
    """Двойной блик бежит по диагонали. t in [0,1)."""
    w, h = base.size
    arr = np.asarray(base.convert("RGBA")).astype(np.float32)
    xs = np.arange(w)[None, :]
    ys = np.arange(h)[:, None]
    # ease: блик чуть замедляется у краёв — движение живее равномерного
    tt = t - 0.12 * math.sin(2 * math.pi * t) / (2 * math.pi)
    band_w = w * 0.26
    center = -band_w * 1.5 + tt * (w + 3 * band_w)
    diag = xs + ys * 0.55
    dist = np.abs(diag - center)
    core = np.clip(1.0 - dist / (band_w * 0.38), 0, 1) ** 2
    soft = np.clip(1.0 - dist / band_w, 0, 1) ** 2
    glow = np.clip(core * 0.85 + soft * 0.35, 0, 1)[..., None]
    alpha = arr[..., 3:4] / 255.0
    # высветляем к белому пропорционально ореолу — цвета не «горят»
    arr[..., :3] = arr[..., :3] + (255.0 - arr[..., :3]) * glow * alpha * 0.9
    return Image.fromarray(np.clip(arr, 0, 255).astype(np.uint8), "RGBA")


def _wave_frame(base: Image.Image, t: float, amp: float = 0.075) -> Image.Image:
    """Флаг: основной синус + вторая гармоника, амплитуда растёт слева направо."""
    w, h = base.size
    src = np.asarray(base.convert("RGBA"))
    out = np.zeros_like(src)
    a = h * amp
    for x in range(w):
        envelope = 0.35 + 0.65 * (x / max(1, w - 1))  # у «древка» почти не колышется
        s = a * envelope * (
            math.sin(2 * math.pi * (t + x / w * 1.2))
            + 0.35 * math.sin(2 * math.pi * (2 * t + x / w * 2.6))
        )
        shift = int(round(s))
        col = np.roll(src[:, x, :], shift, axis=0)
        if shift > 0:
            col[:shift] = 0
        elif shift < 0:
            col[shift:] = 0
        out[:, x, :] = col
    return Image.fromarray(out, "RGBA")


def _pop_frame(base: Image.Image, t: float) -> Image.Image:
    """Дыхание масштаба + лёгкое вертикальное покачивание."""
    w, h = base.size
    scale = 1.0 + 0.075 * math.sin(2 * math.pi * t)
    bob = int(round(h * 0.02 * math.sin(2 * math.pi * t + math.pi / 3)))
    nw, nh = max(1, int(w * scale)), max(1, int(h * scale))
    fr = base.resize((nw, nh), Image.LANCZOS)
    canvas = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    canvas.alpha_composite(fr, ((w - nw) // 2, (h - nh) // 2 + bob))
    return canvas


def _glow_frame(base: Image.Image, t: float) -> Image.Image:
    """Неон: пульсирующий размытый ореол позади + подсветка самой картинки."""
    w, h = base.size
    k = 0.5 + 0.5 * math.sin(2 * math.pi * t)
    # ореол — размытая и высветленная копия картинки
    halo = base.filter(ImageFilter.GaussianBlur(max(4, min(w, h) // 40)))
    harr = np.asarray(halo).astype(np.float32)
    harr[..., :3] = np.clip(harr[..., :3] * 1.6 + 40, 0, 255)
    harr[..., 3] = harr[..., 3] * (0.35 + 0.45 * k)
    halo = Image.fromarray(harr.astype(np.uint8), "RGBA")

    arr = np.asarray(base.convert("RGBA")).astype(np.float32)
    alpha = arr[..., 3:4] / 255.0
    boost = 0.92 + 0.18 * k
    arr[..., :3] = np.clip(arr[..., :3] * boost, 0, 255) * alpha + arr[..., :3] * (1 - alpha)
    lit = Image.fromarray(arr.astype(np.uint8), "RGBA")

    canvas = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    canvas.alpha_composite(halo)
    canvas.alpha_composite(lit)
    return canvas


def _hue_matrix(angle: float) -> np.ndarray:
    """Матрица поворота оттенка в RGB-пространстве."""
    c, s = math.cos(angle), math.sin(angle)
    ot = 1.0 / 3.0
    sq = math.sqrt(ot)
    return np.array([
        [c + (1 - c) * ot, ot * (1 - c) - sq * s, ot * (1 - c) + sq * s],
        [ot * (1 - c) + sq * s, c + ot * (1 - c), ot * (1 - c) - sq * s],
        [ot * (1 - c) - sq * s, ot * (1 - c) + sq * s, c + ot * (1 - c)],
    ], dtype=np.float32)


def _rainbow_frame(base: Image.Image, t: float) -> Image.Image:
    """Полный оборот оттенка за ролик — бесшовно."""
    arr = np.asarray(base.convert("RGBA")).astype(np.float32)
    rgb = arr[..., :3] @ _hue_matrix(2 * math.pi * t).T
    arr[..., :3] = np.clip(rgb, 0, 255)
    return Image.fromarray(arr.astype(np.uint8), "RGBA")


def _shake_frame(base: Image.Image, t: float) -> Image.Image:
    """Упругая тряска. Частоты 2 и 3 — целые, луп бесшовный."""
    w, h = base.size
    dx = int(round(w * 0.018 * math.sin(2 * math.pi * 2 * t)))
    dy = int(round(h * 0.028 * math.sin(2 * math.pi * 3 * t + 1.1)))
    tilt = 2.4 * math.sin(2 * math.pi * t + 0.5)
    fr = base.rotate(tilt, resample=Image.BICUBIC, expand=False)
    canvas = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    canvas.alpha_composite(fr, (dx, dy))
    return canvas


EFFECTS = {
    "sheen": _sheen_frame,
    "wave": _wave_frame,
    "pop": _pop_frame,
    "glow": _glow_frame,
    "rainbow": _rainbow_frame,
    "shake": _shake_frame,
}


def make_frames(base: Image.Image, effect: str, n: int) -> list[Image.Image]:
    fn = EFFECTS.get(effect, _sheen_frame)
    return [fn(base, i / n) for i in range(n)]


def frames_to_webm(frames: list[Image.Image], fps: int = 30) -> bytes:
    tmp = tempfile.mkdtemp(prefix="anim_")
    try:
        for i, f in enumerate(frames):
            f.save(os.path.join(tmp, f"f{i:03d}.png"))
        out = os.path.join(tmp, "o.webm")
        subprocess.run([
            "ffmpeg", "-y", "-framerate", str(fps),
            "-i", os.path.join(tmp, "f%03d.png"),
            "-c:v", "libvpx-vp9", "-pix_fmt", "yuva420p",
            "-auto-alt-ref", "0", "-metadata:s:v:0", "alpha_mode=1",
            "-b:v", "0", "-crf", "28", "-an", out,
        ], check=True, capture_output=True)
        with open(out, "rb") as fh:
            return fh.read()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def animate_text_webm(base: Image.Image, effect: str = "sheen",
                      fps: int = 30, seconds: float = 1.8) -> bytes:
    n = max(8, int(fps * seconds))
    return frames_to_webm(make_frames(base, effect, n), fps)
