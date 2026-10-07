"""
splitter.py — режет любую картинку в сетку 100x100 png-тайлов для custom_emoji.

Логика:
  - подгоняем картинку под сетку columns x rows, где каждая клетка ровно 100x100;
  - сохраняем прозрачность (RGBA), фон НЕ заливаем;
  - возвращаем список тайлов (bytes PNG) построчно (left->right, top->bottom)
    и метаданные сетки, чтобы потом собрать сообщение.

Telegram-лимиты для custom_emoji:
  - размер ровно 100x100;
  - static = .webp/.png, video = .webm (VP9), animated = .tgs;
  - в одном наборе максимум 200 эмодзи;
  - в одном сообщении практический предел ~100 кастом-эмодзи => большие сетки
    бьём на несколько сообщений (по строкам).
"""

from __future__ import annotations
from dataclasses import dataclass
from io import BytesIO
from PIL import Image

TILE = 100  # px, требование Telegram для custom_emoji


@dataclass
class SplitResult:
    tiles: list[bytes]      # PNG-байты каждой клетки, построчно
    cols: int
    rows: int

    @property
    def count(self) -> int:
        return len(self.tiles)


def _fit_to_grid(img: Image.Image, cols: int, mode: str) -> tuple[Image.Image, int, int]:
    """
    Подгоняем картинку так, чтобы ширина = cols*100, а высота кратна 100.
    mode:
      'contain' — вписать целиком (могут появиться прозрачные поля сверху/снизу);
      'cover'   — заполнить и обрезать лишнее по высоте (картинка крупнее, без полей).
    """
    img = img.convert("RGBA")
    target_w = cols * TILE
    scale = target_w / img.width
    new_h = max(TILE, round(img.height * scale))
    img = img.resize((target_w, new_h), Image.LANCZOS)

    rows = max(1, round(new_h / TILE))
    target_h = rows * TILE

    if mode == "cover":
        # обрезаем по центру до кратной высоты
        if new_h > target_h:
            top = (new_h - target_h) // 2
            img = img.crop((0, top, target_w, top + target_h))
        else:
            canvas = Image.new("RGBA", (target_w, target_h), (0, 0, 0, 0))
            canvas.paste(img, (0, (target_h - new_h) // 2))
            img = canvas
    else:  # contain
        canvas = Image.new("RGBA", (target_w, target_h), (0, 0, 0, 0))
        canvas.paste(img, (0, (target_h - new_h) // 2))
        img = canvas

    return img, cols, rows


def split_image(
    data: bytes,
    cols: int = 6,
    mode: str = "cover",
    max_emojis: int = 200,
) -> SplitResult:
    """
    Главная функция. data — байты входной картинки (png/jpg/webp).
    cols — сколько клеток по ширине (рекомендую 5-8). rows считается автоматически.
    """
    img = Image.open(BytesIO(data))
    img, cols, rows = _fit_to_grid(img, cols, mode)

    if cols * rows > max_emojis:
        # ужимаем число строк, чтобы влезть в лимит набора
        max_rows = max(1, max_emojis // cols)
        target_h = max_rows * TILE
        top = (img.height - target_h) // 2 if img.height > target_h else 0
        img = img.crop((0, top, img.width, top + target_h))
        rows = max_rows

    tiles: list[bytes] = []
    for r in range(rows):
        for c in range(cols):
            box = (c * TILE, r * TILE, (c + 1) * TILE, (r + 1) * TILE)
            cell = img.crop(box)
            buf = BytesIO()
            cell.save(buf, format="PNG")  # png держит альфу; Telegram примет как static
            tiles.append(buf.getvalue())

    return SplitResult(tiles=tiles, cols=cols, rows=rows)


def preview_grid(result: SplitResult) -> bytes:
    """Склеить тайлы обратно в одну картинку — для визуальной проверки/превью."""
    w, h = result.cols * TILE, result.rows * TILE
    canvas = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    for i, t in enumerate(result.tiles):
        r, c = divmod(i, result.cols)
        canvas.paste(Image.open(BytesIO(t)), (c * TILE, r * TILE))
    buf = BytesIO()
    canvas.save(buf, format="PNG")
    return buf.getvalue()
