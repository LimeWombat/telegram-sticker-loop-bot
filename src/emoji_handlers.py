"""
emoji_handlers.py — режимы StickerLoopBot поверх кастом-эмодзи/стикеров.

Режимы:
  • Текст-карточка: пишешь текст → превью + кнопки (стиль/аним/формат) → собрать:
      - 📦 стикерпак (статик webp + анимир webm),
      - 😎 одиночная анимир. кастом-эмодзи 100×100 (полный текст, без обрезки),
      - 🔠 БОЛЬШОЙ: текст нарезан в сетку кастом-эмодзи, собирается цельным в чате.
  • Фото → нарезка в сетку кастом-эмодзи (выбор ширины 4/6/8).
Видео/гиф/стикеры по-прежнему идут в луп (в bot.py).
"""
from __future__ import annotations

import asyncio
import html
import logging
import os
import re
import time
from io import BytesIO

from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputMediaPhoto,
    InputSticker,
    Message,
    MessageEntity,
    Update,
)
from telegram.constants import StickerType, StickerFormat
from telegram.error import BadRequest, RetryAfter, TimedOut
from telegram.ext import ContextTypes

# щедрые таймауты: аплоад стикеров бывает медленным
API_KW = dict(read_timeout=60, write_timeout=60, connect_timeout=30, pool_timeout=60)

from src.render_text import render_text_image, to_png, to_webp, STYLES, DEFAULT_STYLE
from src.animate import animate_text_webm, EFFECTS
from src.splitter import split_image

log = logging.getLogger(__name__)

MAX_SET_EMOJI = 200
CREATE_BATCH = 50
PLACEHOLDER = "🟩"
PLACEHOLDER_LEN16 = 2
MAX_PER_MSG = 100
GRID_CHOICES = (4, 6, 8)
DEFAULT_COLS = 6
ANIM_CHOICES = [
    ("sheen", "✨ Блик"), ("wave", "🌊 Волна"), ("pop", "💥 Пульс"),
    ("glow", "🔆 Неон"), ("rainbow", "🌈 Радуга"), ("shake", "🫨 Тряска"),
]
ANIM_LABELS = dict(ANIM_CHOICES)
DEFAULT_ANIM = "sheen"
MAX_TEXT = 200

# user_id -> {"text","style","anim"} — состояние текст-карточки
TEXT_STATE: dict[int, dict] = {}
# user_id -> file_id картинки, ждущей выбора сетки
PENDING_SPLIT: dict[int, str] = {}


# ── set helpers ────────────────────────────────────────

async def _log_admin(bot, text: str) -> None:
    """Лог в админ-чат (LOG_CHAT_ID). Тихо молчит, если не настроен."""
    raw = os.getenv("LOG_CHAT_ID", "").strip()
    if not raw:
        return
    try:
        chat_id: int | str = int(raw)
    except ValueError:
        chat_id = raw
    try:
        await bot.send_message(
            chat_id=chat_id, text=text[:3900], parse_mode="HTML",
            disable_web_page_preview=True, read_timeout=20,
        )
    except Exception:
        log.exception("admin log failed")


def _user_link(user_id: int) -> str:
    return f'👤 <a href="tg://user?id={user_id}"><code>{user_id}</code></a>'

async def _create_emoji_set(bot, user_id, name, title, stickers) -> None:
    await bot.create_new_sticker_set(
        user_id=user_id, name=name, title=title,
        stickers=stickers, sticker_type=StickerType.CUSTOM_EMOJI, **API_KW,
    )


async def _add_one(bot, **kwargs) -> None:
    """add_sticker_to_set с обработкой флуд-контроля и лага создания набора."""
    invalid_tries = 0
    while True:
        try:
            await bot.add_sticker_to_set(**kwargs, **API_KW)
            return
        except RetryAfter as e:
            await asyncio.sleep(float(getattr(e, "retry_after", 3)) + 0.5)
        except BadRequest as e:
            # свежесозданный набор доезжает до Bot API с лагом — Stickerset_invalid лечится повтором
            invalid_tries += 1
            if "stickerset_invalid" not in str(e).lower() or invalid_tries > 5:
                raise
            await asyncio.sleep(invalid_tries)


async def _add_tiles(bot, user_id, name, title, tiles: list[bytes], progress=None) -> None:
    # создаём набор ОДНОЙ плиткой (быстрый запрос), остальные добавляем по одной —
    # иначе аплоад десятков файлов одним вызовом ловит write-timeout.
    first = InputSticker(sticker=tiles[0], emoji_list=["🧩"], format=StickerFormat.STATIC)
    await bot.create_new_sticker_set(
        user_id=user_id, name=name, title=title,
        stickers=[first], sticker_type=StickerType.CUSTOM_EMOJI, **API_KW,
    )
    total = len(tiles)
    added = 1
    timeouts = 0
    while added < total:
        s = InputSticker(sticker=tiles[added], emoji_list=["🧩"], format=StickerFormat.STATIC)
        try:
            await _add_one(bot, user_id=user_id, name=name, sticker=s)
            added += 1
            timeouts = 0
        except TimedOut:
            # запрос мог пройти на стороне Telegram — сверяем фактический размер набора
            timeouts += 1
            if timeouts > 5:
                raise
            await asyncio.sleep(2)
            sset = await bot.get_sticker_set(name=name, read_timeout=30)
            added = max(added, len(sset.stickers))
            continue
        if progress and added % 8 == 0:
            await progress(added, total)
        await asyncio.sleep(0.4)


async def _send_emoji_grid(bot, chat_id, ids: list[str], cols: int, rows: int) -> None:
    rows_per_msg = max(1, MAX_PER_MSG // cols)
    for r0 in range(0, rows, rows_per_msg):
        chunk = range(r0, min(r0 + rows_per_msg, rows))
        parts, entities, offset = [], [], 0
        for r in chunk:
            for c in range(cols):
                idx = r * cols + c
                if idx >= len(ids):
                    continue
                parts.append(PLACEHOLDER)
                entities.append(MessageEntity(
                    type=MessageEntity.CUSTOM_EMOJI, offset=offset,
                    length=PLACEHOLDER_LEN16, custom_emoji_id=ids[idx],
                ))
                offset += PLACEHOLDER_LEN16
            parts.append("\n")
            offset += 1
        await bot.send_message(chat_id=chat_id, text="".join(parts), entities=entities)


def _slug(prefix, user_id, username):
    return f"{prefix}{user_id}_{int(time.time())}_by_{username}"[:64]


# ── split core (photo / big text) ──────────────────────

def is_still_image(message: Message) -> bool:
    if getattr(message, "photo", None):
        return True
    doc = getattr(message, "document", None)
    if doc:
        mt = doc.mime_type or ""
        if mt.startswith("image/") and mt != "image/gif":
            return True
    return False


async def _split_to_emoji(bot, chat_id, user_id, image_bytes, cols, status, mode="cover"):
    me = await bot.get_me()
    res = split_image(image_bytes, cols=cols, mode=mode, max_emojis=MAX_SET_EMOJI)
    await status.edit_text(f"✂️ Сетка {res.cols}×{res.rows} = {res.count} эмодзи, собираю набор…")
    name = _slug("b", user_id, me.username)

    async def _prog(done, total):
        try:
            await status.edit_text(f"📦 Добавляю эмодзи… {done}/{total}")
        except Exception:
            pass

    await _add_tiles(bot, user_id, name, "pack", res.tiles, progress=_prog)
    sset = await bot.get_sticker_set(name=name, read_timeout=30)
    ids = [s.custom_emoji_id for s in sset.stickers]
    await status.edit_text(f"✅ Набор 👉 https://t.me/addemoji/{name}\nСобираю в чате…")
    await _send_emoji_grid(bot, chat_id, ids, res.cols, res.rows)
    await _log_admin(
        bot,
        f"🧩 <b>Эмодзи-пак создан</b>\n{_user_link(user_id)}\n"
        f"🔗 t.me/addemoji/{name} · {res.count} шт ({res.cols}×{res.rows})",
    )


# ── photo → grid-width buttons → split ─────────────────

def _grid_keyboard(default=DEFAULT_COLS) -> InlineKeyboardMarkup:
    btns = [InlineKeyboardButton(f"✂️ ×{n}" + (" ✓" if n == default else ""), callback_data=f"egrid:{n}")
            for n in GRID_CHOICES]
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🔁 Сделать GIF-луп", callback_data="egrid:loop")],
        btns,
    ])


async def offer_grid(message: Message, file_id: str, user_id: int) -> None:
    PENDING_SPLIT[user_id] = file_id
    await message.reply_text(
        "Что сделать с картинкой?\n\n"
        "🔁 <b>GIF-луп</b> — зациклю с фоном из настроек\n"
        "✂️ <b>Нарезка</b> — пак кастом-эмодзи, в чате соберётся обратно "
        "(×4/×6/×8 — ширина сетки, больше клеток = чётче)",
        parse_mode="HTML",
        reply_markup=_grid_keyboard(),
    )


async def grid_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not query or not query.from_user or not query.message:
        return
    await query.answer()
    user_id = query.from_user.id
    try:
        cols = int((query.data or "").split(":")[1])
    except (ValueError, IndexError):
        return
    if cols not in GRID_CHOICES:
        cols = DEFAULT_COLS
    file_id = PENDING_SPLIT.pop(user_id, None)
    if not file_id:
        await query.edit_message_text("Картинка потерялась 🤷 Пришли фото заново.")
        return
    bot = context.bot
    try:
        await query.edit_message_text("📥 Скачиваю картинку…")
        f = await bot.get_file(file_id)
        buf = BytesIO()
        await f.download_to_memory(buf)
        await _split_to_emoji(bot, query.message.chat_id, user_id, buf.getvalue(), cols, query.message)
    except Exception as e:
        log.exception("grid_callback failed")
        PENDING_SPLIT[user_id] = file_id  # вернуть картинку — повторный клик по кнопке сработает
        try:
            await query.message.reply_text(f"❌ Ошибка: {e}")
        except Exception:
            pass


# ── text card ──────────────────────────────────────────

def _card_keyboard(style: str, anim: str) -> InlineKeyboardMarkup:
    rows = []
    # стили — по 3 в ряд, активный помечен галкой
    keys = list(STYLES.keys())
    for i in range(0, len(keys), 3):
        rows.append([
            InlineKeyboardButton(("✓ " if k == style else "") + STYLES[k].label,
                                 callback_data=f"etxt:style:{k}")
            for k in keys[i:i + 3]
        ])
    # анимации — по 3 в ряд
    for i in range(0, len(ANIM_CHOICES), 3):
        rows.append([
            InlineKeyboardButton(("✓ " if a == anim else "") + lbl, callback_data=f"etxt:anim:{a}")
            for a, lbl in ANIM_CHOICES[i:i + 3]
        ])
    # действия
    rows.append([
        InlineKeyboardButton("📦 Собрать стикерпак", callback_data="etxt:make:badge"),
        InlineKeyboardButton("😎 Собрать эмодзи", callback_data="etxt:make:emoji"),
    ])
    rows.append([
        InlineKeyboardButton("🔠 Баннер ×4", callback_data="etxt:make:big4"),
        InlineKeyboardButton("🔠 ×6", callback_data="etxt:make:big6"),
        InlineKeyboardButton("🔠 ×8", callback_data="etxt:make:big8"),
    ])
    return InlineKeyboardMarkup(rows)


def _preview_png(text: str, style: str) -> bytes:
    return to_png(render_text_image(text, style, width=512))


def _card_caption(text: str, style: str, anim: str) -> str:
    return (
        f"🎨 <b>«{html.escape(text[:40])}»</b>\n"
        f"Стиль: <b>{html.escape(STYLES[style].label)}</b> · "
        f"Анимация: <b>{html.escape(ANIM_LABELS.get(anim, anim))}</b>\n\n"
        "1️⃣ Выбери стиль и анимацию — превью обновится\n"
        "2️⃣ Жми, что собрать:\n"
        "📦 стикерпак (статика + анимация) · 😎 анимир. эмодзи 100×100\n"
        "🔠 баннер из кастом-эмодзи (×4/×6/×8 — ширина сетки)"
    )


async def show_text_card(message: Message, user_id: int, text: str) -> None:
    text = text.strip()[:MAX_TEXT]
    st = TEXT_STATE.get(user_id, {})
    style = st.get("style", DEFAULT_STYLE)
    anim = st.get("anim", DEFAULT_ANIM)
    TEXT_STATE[user_id] = {"text": text, "style": style, "anim": anim}
    await message.reply_photo(
        photo=BytesIO(_preview_png(text, style)),
        caption=_card_caption(text, style, anim),
        parse_mode="HTML",
        reply_markup=_card_keyboard(style, anim),
    )


async def text_card_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not query or not query.from_user:
        return
    user_id = query.from_user.id
    data = query.data or ""
    st = TEXT_STATE.get(user_id)
    if not st:
        await query.answer("Карточка устарела, пришли текст заново.", show_alert=True)
        return
    bot = context.bot
    parts = data.split(":")  # etxt:style:lime / etxt:anim:wave / etxt:make:big6

    if parts[1] == "style":
        st["style"] = parts[2] if parts[2] in STYLES else DEFAULT_STYLE
        await query.answer(STYLES[st["style"]].label)
        try:
            await query.edit_message_media(
                media=InputMediaPhoto(
                    media=BytesIO(_preview_png(st["text"], st["style"])),
                    caption=_card_caption(st["text"], st["style"], st["anim"]),
                    parse_mode="HTML",
                ),
                reply_markup=_card_keyboard(st["style"], st["anim"]),
            )
        except Exception:
            pass
        return

    if parts[1] == "anim":
        st["anim"] = parts[2] if parts[2] in EFFECTS else DEFAULT_ANIM
        await query.answer(f"Анимация: {st['anim']}")
        try:
            await query.edit_message_reply_markup(reply_markup=_card_keyboard(st["style"], st["anim"]))
        except Exception:
            pass
        return

    if parts[1] == "make":
        if not query.message:
            await query.answer("Карточка недоступна, пришли текст заново.", show_alert=True)
            return
        await query.answer("Делаю…")
        kind = parts[2]
        status = await query.message.reply_text("⏳ Рендерю…")
        try:
            await _make(bot, query.message.chat_id, user_id, st, kind, status)
        except Exception as e:
            log.exception("text make failed")
            try:
                await status.edit_text(f"❌ Ошибка: {e}")
            except Exception:
                pass


async def _make(bot, chat_id, user_id, st, kind, status) -> None:
    me = await bot.get_me()
    text, style, anim = st["text"], st["style"], st["anim"]

    if kind == "badge":
        await status.edit_text("🎨 Рендерю стикерпак…")
        base = render_text_image(text, style, width=512)
        static = to_webp(base)
        webm = animate_text_webm(base, anim)
        name = _slug("s", user_id, me.username)
        await bot.create_new_sticker_set(
            user_id=user_id, name=name, title=f"{text[:40]} — @{me.username}",
            stickers=[
                InputSticker(sticker=static, emoji_list=["🔥"], format=StickerFormat.STATIC),
                InputSticker(sticker=webm, emoji_list=["✨"], format=StickerFormat.VIDEO),
            ],
            sticker_type=StickerType.REGULAR, **API_KW,
        )
        await status.edit_text(f"✅ Стикерпак 👉 https://t.me/addstickers/{name}")
        await _log_admin(
            bot,
            f"📦 <b>Стикерпак из текста</b>\n{_user_link(user_id)}\n"
            f"🔗 t.me/addstickers/{name}\n"
            f"✍ «{html.escape(text[:60])}» · стиль {style} · аним {anim}",
        )

    elif kind == "emoji":
        await status.edit_text("🎨 Рендерю эмодзи…")
        base = render_text_image(text, style, width=100, height=100)
        webm = animate_text_webm(base, anim, seconds=1.6)
        name = _slug("e", user_id, me.username)
        await _create_emoji_set(bot, user_id, name, f"{text[:40]} — @{me.username}",
                                [InputSticker(sticker=webm, emoji_list=["✨"], format=StickerFormat.VIDEO)])
        await status.edit_text(f"✅ Эмодзи-набор 👉 https://t.me/addemoji/{name}\n"
                               "Поставь в чате — анимир. кастом-эмодзи с твоим текстом.")
        await _log_admin(
            bot,
            f"😎 <b>Эмодзи из текста</b>\n{_user_link(user_id)}\n"
            f"🔗 t.me/addemoji/{name}\n"
            f"✍ «{html.escape(text[:60])}» · стиль {style} · аним {anim}",
        )

    elif kind.startswith("big"):
        cols = int(kind[3:]) if kind[3:].isdigit() else 6
        await status.edit_text(f"🔠 Рендерю большой текст (сетка {cols} в ширину)…")
        img = render_text_image(text, style, width=cols * 100)
        png = to_png(img)
        await _split_to_emoji(bot, chat_id, user_id, png, cols, status, mode="contain")


# ── /команды (дублируют карточку для быстрого доступа) ──

async def text_entry(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Точка входа из on_text: показать карточку по обычному тексту."""
    if not update.message or not update.effective_user:
        return
    text = (update.message.text or "").strip()
    if not text:
        return
    await show_text_card(update.message, update.effective_user.id, text)


async def emoji_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not update.effective_user:
        return
    text = re.sub(r'^/emoji(?:@\w+)?\s*', '', (update.message.text or "")).strip()
    if not text:
        await update.message.reply_text("Напиши текст: /emoji ПРИВЕТ НАРОД")
        return
    await show_text_card(update.message, update.effective_user.id, text)


async def badge_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not update.effective_user:
        return
    text = re.sub(r'^/badge(?:@\w+)?\s*', '', (update.message.text or "")).strip()
    if not text:
        await update.message.reply_text("Напиши текст: /badge АНОНС")
        return
    await show_text_card(update.message, update.effective_user.id, text)


async def split_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not update.effective_user:
        return
    msg = update.message
    replied = msg.reply_to_message
    if not replied:
        await msg.reply_text("Ответь командой /split на картинку, которую нарезать в эмодзи-пак.")
        return
    file = None
    if replied.photo:
        file = await replied.photo[-1].get_file()
    elif replied.document and (replied.document.mime_type or "").startswith("image/"):
        file = await replied.document.get_file()
    elif replied.sticker and not replied.sticker.is_video and not replied.sticker.is_animated:
        file = await replied.sticker.get_file()
    if not file:
        await msg.reply_text("Ответь на картинку, фото или статичный стикер.")
        return
    status = await msg.reply_text("📥 Скачиваю…")
    try:
        buf = BytesIO()
        await file.download_to_memory(buf)
        await _split_to_emoji(context.bot, msg.chat_id, update.effective_user.id,
                              buf.getvalue(), DEFAULT_COLS, status)
    except Exception as e:
        log.exception("split_command failed")
        await status.edit_text(f"❌ Ошибка: {e}")
