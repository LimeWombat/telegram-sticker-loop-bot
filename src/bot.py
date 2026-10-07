from __future__ import annotations

import asyncio
from collections import deque
import html
import secrets
import sqlite3
import json
import logging
import logging.handlers
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Iterable, Sequence

from telegram import (
    BotCommandScopeChat,
    BotCommandScopeChatAdministrators,
    BotCommandScopeDefault,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InlineQueryResultArticle,
    InlineQueryResultCachedVideo,
    InputMediaPhoto,
    InputMediaAnimation,
    InputMediaDocument,
    InputMediaVideo,
    InputTextMessageContent,
    Message,
    Sticker,
    Update,
)
from telegram.constants import ChatAction, ParseMode
from telegram.error import BadRequest, Conflict, Forbidden, NetworkError, RetryAfter, TelegramError
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    InlineQueryHandler,
    MessageHandler,
    filters,
)

from src.i18n import LANGUAGES, LANGUAGE, normalize_language, t

ROOT = Path(__file__).resolve().parents[1]
VAR_DIR = ROOT / "var"
RUNS_DIR = VAR_DIR / "runs"
BG_DIR = VAR_DIR / "backgrounds"
LOG_DIR = ROOT / "logs"
ENV_PATH = ROOT / ".env"
LIMITS_STATE_PATH = VAR_DIR / "limits.json"
DB_PATH = VAR_DIR / "bot.sqlite3"
MENU_ASSETS_PATH = VAR_DIR / "menu_assets.json"

# Animated custom emoji from FlagsPack; Indonesia is from RestrictedEmoji.
COUNTRIES = {
    "RU": ("Россия", "ru", "5467784510756104319"),
    "US": ("United States", "en", "5465318847340882369"),
    "IR": ("ایران", "fa", "5305571086409152543"),
    "SA": ("السعودية", "ar", "5465447906813159441"),
    "TR": ("Türkiye", "tr", "5235921989771736755"),
    "ES": ("España", "es", "5465387605472323114"),
    "BR": ("Brasil", "pt-br", "5467414838625969618"),
    "ID": ("Indonesia", "id", "5291937150814661333"),
}

BACKGROUND_PRESETS = {
    "dark": ("Dark", "#080a0f"),
    "black": ("Black", "#000000"),
    "graphite": ("Graphite", "#111827"),
    "white": ("White", "#f8fafc"),
    "blue": ("Blue", "#0f172a"),
    "green": ("Green", "#052e2b"),
}

RESOLUTION_PRESETS = {
    "512x512": (512, 512, 30),
    "640x360": (640, 360, 30),
    "1280x720": (1280, 720, 30),
    "1920x1080": (1920, 1080, 30),
    "1920x600": (1920, 600, 30),
}

ITEM_COLOR_PRESETS = {
    "white": ("Белый", "#ffffff"),
    "black": ("Чёрный", "#000000"),
    "red": ("Красный", "#e74c3c"),
    "blue": ("Синий", "#3498db"),
    "green": ("Зелёный", "#2ecc71"),
    "gold": ("Золотой", "#f1c40f"),
    "pink": ("Розовый", "#e91e90"),
    "orange": ("Оранж", "#ff9800"),
}

GRADIENT_PRESETS = [
    ("Закат", "#ff512f", "#dd2476", "h"),
    ("Океан", "#2193b0", "#6dd5ed", "h"),
    ("Лес", "#11998e", "#38ef7d", "h"),
    ("Фиолет", "#8e2de2", "#4a00e0", "h"),
    ("Неон", "#f857a6", "#ff5858", "h"),
    ("Ночь", "#0f0c29", "#302b63", "v"),
    ("Мята", "#00b4db", "#0083b0", "h"),
    ("Лава", "#cb2d3e", "#ef3b3c", "h"),
]



@dataclass(frozen=True)
class RenderSettings:
    background_key: str
    background_hex: str
    gradient_end_hex: str | None
    gradient_direction: str | None
    width: int
    height: int
    sticker_size: int
    fps: int
    static_seconds: float
    output_format: str
    item_color_hex: str | None
    notes: str
    watermark_enabled: bool
    watermark_text: str


@dataclass(frozen=True)
class SourceRef:
    file_id: str
    label: str


@dataclass(frozen=True)
class ResultRef:
    source: SourceRef
    chat_id: int
    message_id: int
    file_id: str | None = None
    output_format: str = "gif"
    caption: str = ""
    caption_html: bool = False


@dataclass(frozen=True)
class SafetyConfig:
    max_global_renders: int
    per_user_window_jobs: int
    per_user_window_seconds: int
    per_user_min_gap_seconds: int
    spam_events_before_ban: int
    spam_window_seconds: int
    ban_seconds: int
    max_source_bytes: int
    max_output_bytes: int
    render_timeout_seconds: int
    runs_retention_seconds: int


@dataclass
class UserLimitState:
    render_times: deque[float]
    violation_times: deque[float]
    banned_until: float = 0.0


@dataclass(frozen=True)
class BroadcastDraft:
    sender_id: int
    created_at: float
    target_user_ids: tuple[int, ...]
    text: str | None = None
    copy_from_chat_id: int | None = None
    copy_message_id: int | None = None


@dataclass(frozen=True)
class PendingAction:
    action: str
    chat_id: int
    message_id: int
    surface: str


class UserFacingError(Exception):
    pass


class RenderGate:
    def __init__(self, limit: int) -> None:
        self.limit = max(1, limit)
        self.active = 0
        self._lock = asyncio.Lock()

    async def try_acquire(self) -> bool:
        async with self._lock:
            if self.active >= self.limit:
                return False
            self.active += 1
            return True

    async def release(self) -> None:
        async with self._lock:
            self.active = max(0, self.active - 1)


USER_SETTINGS: dict[int, RenderSettings] = {}
USER_BG_IMAGES: dict[int, Path] = {}
LAST_SOURCE: dict[int, SourceRef] = {}
LAST_RESULT: dict[int, ResultRef] = {}
REFRESH_TASKS: dict[int, asyncio.Task] = {}
REFRESH_REQUESTS: dict[int, tuple[Message, int]] = {}
REFRESH_REVISIONS: dict[int, int] = {}
MISSING_RESULT_NOTICE: set[int] = set()
PENDING_ACTIONS: dict[int, PendingAction] = {}
BUSY: set[int] = set()
USER_LIMITS: dict[int, UserLimitState] = {}
GLOBAL_RENDER_GATE: RenderGate | None = None
BROADCAST_DRAFTS: dict[str, BroadcastDraft] = {}
HAS_DRAWTEXT_FILTER: bool | None = None
MENU_ASSETS: list[str] = []
MENU_SECTION_ASSETS: dict[str, list[str]] = {}

MENU_ASSET_SECTIONS = {
    "main": "главное меню",
    "palette": "палитра",
    "bg": "фон",
    "resolution": "разрешение",
    "format": "формат",
    "item_color": "цвет emoji",
    "notes": "заметки",
    "watermark": "вотермарка",
}


def load_dotenv(path: Path) -> None:
    if not path.exists():
        return

    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        os.environ.setdefault(key, value)


def env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except ValueError:
        return default


def env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)))
    except ValueError:
        return default


def env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "y", "on"}


def env_str(name: str, default: str) -> str:
    value = os.getenv(name)
    if value is None or value == "":
        return default
    return value


def clamp(value: float, minimum: float, maximum: float) -> float:
    return max(minimum, min(maximum, value))


def parse_int_list(raw: str | None) -> set[int]:
    if not raw:
        return set()
    values = set()
    for part in raw.split(","):
        item = part.strip()
        if not item:
            continue
        try:
            values.add(int(item))
        except ValueError:
            logging.warning("Ignoring invalid integer in env list: %s", item)
    return values


def log_chat_id() -> int | str | None:
    raw = os.getenv("LOG_CHAT_ID", "").strip()
    if not raw:
        return None
    try:
        return int(raw)
    except ValueError:
        return raw


def safety_config() -> SafetyConfig:
    return SafetyConfig(
        max_global_renders=env_int("MAX_GLOBAL_RENDERS", 2),
        per_user_window_jobs=env_int("PER_USER_WINDOW_JOBS", 2),
        per_user_window_seconds=env_int("PER_USER_WINDOW_SECONDS", 60),
        per_user_min_gap_seconds=env_int("PER_USER_MIN_GAP_SECONDS", 5),
        spam_events_before_ban=env_int("SPAM_EVENTS_BEFORE_BAN", 6),
        spam_window_seconds=env_int("SPAM_WINDOW_SECONDS", 60),
        ban_seconds=env_int("BAN_SECONDS", 3600),
        max_source_bytes=env_int("MAX_SOURCE_BYTES", 10 * 1024 * 1024),
        max_output_bytes=env_int("MAX_OUTPUT_BYTES", 45 * 1024 * 1024),
        render_timeout_seconds=env_int("RENDER_TIMEOUT_SECONDS", 75),
        runs_retention_seconds=env_int("RUNS_RETENTION_SECONDS", 12 * 60 * 60),
    )


def default_settings() -> RenderSettings:
    key = os.getenv("DEFAULT_BACKGROUND", "dark")
    label, color = BACKGROUND_PRESETS.get(key, BACKGROUND_PRESETS["dark"])
    return RenderSettings(
        background_key=key if key in BACKGROUND_PRESETS else label.lower(),
        background_hex=color,
        gradient_end_hex=None,
        gradient_direction=None,
        width=env_int("OUTPUT_WIDTH", 640),
        height=env_int("OUTPUT_HEIGHT", 360),
        sticker_size=env_int("STICKER_SIZE", 220),
        fps=env_int("OUTPUT_FPS", 30),
        static_seconds=env_float("STATIC_SECONDS", 2.0),
        output_format="gif",
        item_color_hex=None,
        notes="",
        watermark_enabled=env_bool("WATERMARK_ENABLED", False),
        watermark_text=env_str("WATERMARK_TEXT", "StickerLoop").strip(),
    )


def settings_for(user_id: int) -> RenderSettings:
    current = USER_SETTINGS.setdefault(user_id, default_settings())
    if current.output_format != "gif":
        current = replace(current, output_format="gif")
        USER_SETTINGS[user_id] = current
    return current


def update_settings(user_id: int, **changes) -> RenderSettings:
    current = settings_for(user_id)
    updated = replace(current, **changes)
    USER_SETTINGS[user_id] = updated
    return updated


def setup_logging() -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=os.getenv("LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        handlers=[
            logging.StreamHandler(),
            logging.handlers.RotatingFileHandler(
                LOG_DIR / "bot.log",
                maxBytes=5 * 1024 * 1024,
                backupCount=3,
                encoding="utf-8",
            ),
        ],
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    # рестарты приложения — не событие, держим тише
    logging.getLogger("telegram.ext.Application").setLevel(logging.WARNING)


def db_connect() -> sqlite3.Connection:
    VAR_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=15)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=15000")
    return conn


def init_db() -> None:
    with db_connect() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                username TEXT,
                first_name TEXT,
                last_name TEXT,
                language_code TEXT,
                is_bot INTEGER NOT NULL DEFAULT 0,
                first_seen INTEGER NOT NULL,
                last_seen INTEGER NOT NULL,
                message_count INTEGER NOT NULL DEFAULT 0,
                render_count INTEGER NOT NULL DEFAULT 0,
                last_action TEXT,
                blocked_at INTEGER
            )
            """
        )
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_users_blocked_last_seen
            ON users(blocked_at, last_seen)
            """
        )
        columns = {row["name"] for row in conn.execute("PRAGMA table_info(users)")}
        for column in ("ui_language", "country_code"):
            if column not in columns:
                conn.execute(f"ALTER TABLE users ADD COLUMN {column} TEXT")
        conn.execute(
            "CREATE TABLE IF NOT EXISTS render_sessions ("
            "user_id INTEGER PRIMARY KEY, settings TEXT NOT NULL, result TEXT, has_background INTEGER NOT NULL)"
        )


def save_render_session(user_id: int) -> None:
    with db_connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        result = LAST_RESULT.get(user_id)
        conn.execute(
            "INSERT INTO render_sessions(user_id, settings, result, has_background) VALUES (?, ?, ?, ?) "
            "ON CONFLICT(user_id) DO UPDATE SET settings=excluded.settings, result=excluded.result, "
            "has_background=excluded.has_background",
            (user_id, json.dumps(asdict(settings_for(user_id))),
             json.dumps(asdict(result)) if result else None, int(user_id in USER_BG_IMAGES)),
        )


def load_render_sessions() -> None:
    with db_connect() as conn:
        rows = conn.execute("SELECT * FROM render_sessions").fetchall()
    for row in rows:
        try:
            user_id = row["user_id"]
            USER_SETTINGS[user_id] = RenderSettings(**json.loads(row["settings"]))
            if row["result"]:
                saved = json.loads(row["result"])
                saved["source"] = SourceRef(**saved["source"])
                LAST_RESULT[user_id] = ResultRef(**saved)
                LAST_SOURCE[user_id] = LAST_RESULT[user_id].source
            background = BG_DIR / f"{user_id}.jpg"
            if row["has_background"] and background.is_file():
                USER_BG_IMAGES[user_id] = background
        except (TypeError, ValueError, KeyError):
            logging.exception("Failed to load a saved render session")


def settings_signature(user_id: int) -> tuple:
    background = USER_BG_IMAGES.get(user_id)
    modified = background.stat().st_mtime_ns if background and background.is_file() else None
    return settings_for(user_id), background, modified


def selected_language(user_id: int) -> str | None:
    with db_connect() as conn:
        row = conn.execute("SELECT ui_language FROM users WHERE user_id = ?", (user_id,)).fetchone()
    return row["ui_language"] if row and row["ui_language"] in LANGUAGES else None


def upsert_user(user, action: str, *, render_started: bool = False) -> bool:
    now = int(time.time())
    with db_connect() as conn:
        existing = conn.execute(
            "SELECT user_id FROM users WHERE user_id = ?",
            (user.id,),
        ).fetchone()
        conn.execute(
            """
            INSERT INTO users (
                user_id, username, first_name, last_name, language_code, is_bot,
                first_seen, last_seen, message_count, render_count, last_action
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                username = excluded.username,
                first_name = excluded.first_name,
                last_name = excluded.last_name,
                language_code = excluded.language_code,
                is_bot = excluded.is_bot,
                last_seen = excluded.last_seen,
                message_count = users.message_count + 1,
                last_action = excluded.last_action,
                blocked_at = NULL
            """,
            (
                user.id,
                user.username,
                user.first_name,
                user.last_name,
                user.language_code,
                int(user.is_bot),
                now,
                now,
                0,
                action,
            ),
        )
        return existing is None


def mark_user_blocked(user_id: int) -> None:
    with db_connect() as conn:
        conn.execute(
            "UPDATE users SET blocked_at = ?, last_seen = ? WHERE user_id = ?",
            (int(time.time()), int(time.time()), user_id),
        )


def known_user_ids() -> list[int]:
    with db_connect() as conn:
        rows = conn.execute(
            "SELECT user_id FROM users WHERE blocked_at IS NULL ORDER BY first_seen"
        ).fetchall()
    return [int(row["user_id"]) for row in rows]


def user_stats() -> dict[str, int]:
    now = int(time.time())
    week_ago = now - 604800
    with db_connect() as conn:
        row = conn.execute(
            """
            SELECT
                COUNT(*) AS total,
                SUM(CASE WHEN blocked_at IS NULL THEN 1 ELSE 0 END) AS reachable,
                SUM(render_count) AS renders,
                SUM(CASE WHEN first_seen >= ? THEN 1 ELSE 0 END) AS new_week,
                SUM(CASE WHEN last_seen >= ? AND render_count > 0 THEN 1 ELSE 0 END) AS active_week,
                SUM(CASE WHEN last_seen >= ? AND render_count > 0 THEN render_count ELSE 0 END) AS renders_week
            FROM users
            """,
            (week_ago, week_ago, week_ago),
        ).fetchone()
    return {
        "total": int(row["total"] or 0),
        "reachable": int(row["reachable"] or 0),
        "renders": int(row["renders"] or 0),
        "new_week": int(row["new_week"] or 0),
        "active_week": int(row["active_week"] or 0),
        "renders_week": int(row["renders_week"] or 0),
    }


def user_display(user) -> str:
    parts = [str(user.id)]
    if user.username:
        parts.append(f"@{user.username}")
    name = " ".join(part for part in [user.first_name, user.last_name] if part)
    if name:
        parts.append(name)
    return " | ".join(parts)


def user_html(user) -> str:
    """Кликабельная строка юзера для админ-лога."""
    name = html.escape(
        " ".join(part for part in [user.first_name, user.last_name] if part) or "без имени"
    )
    tag = f" · @{user.username}" if user.username else ""
    return f'{tg_emoji("users")} <a href="tg://user?id={user.id}">{name}</a>{tag} · <code>{user.id}</code>'


async def log_to_owner_chat(context: ContextTypes.DEFAULT_TYPE, text: str) -> None:
    target = log_chat_id()
    if not target:
        return
    try:
        await context.bot.send_message(
            chat_id=target,
            text=text[:3900],
            parse_mode=ParseMode.HTML,
            disable_web_page_preview=True,
            read_timeout=20,
            connect_timeout=20,
        )
    except TelegramError:
        logging.exception("Failed to send owner log")


async def copy_render_source_to_owner_chat(context: ContextTypes.DEFAULT_TYPE, message: Message) -> None:
    target = log_chat_id()
    if not target or not env_bool("LOG_RENDER_SOURCE_PREVIEW", True):
        return
    try:
        await context.bot.copy_message(
            chat_id=target,
            from_chat_id=message.chat_id,
            message_id=message.message_id,
            read_timeout=20,
            connect_timeout=20,
        )
    except TelegramError:
        logging.exception("Failed to copy render source to owner log")


async def remember_user(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    action: str,
    *,
    render_started: bool = False,
    source_label: str | None = None,
    source_message: Message | None = None,
) -> None:
    user = update.effective_user
    if not user:
        return

    is_new = await asyncio.to_thread(upsert_user, user, action, render_started=render_started)
    if is_new and env_bool("LOG_NEW_USERS", True):
        stats = await asyncio.to_thread(user_stats)
        premium = f" · {tg_emoji('stars')} premium" if getattr(user, "is_premium", False) else ""
        await log_to_owner_chat(
            context,
            f"{tg_emoji('new')} <b>Новый юзер #{stats['total']}</b>\n"
            f"{user_html(user)}\n"
            f"{tg_emoji('globe')} {html.escape(user.language_code or '—')}{premium} · вход: <i>{html.escape(action)}</i>",
        )
    if render_started and env_bool("LOG_RENDER_REQUESTS", True):
        await log_to_owner_chat(
            context,
            f"{tg_emoji('gif')} <b>Рендер запрошен</b>\n"
            f"{user_html(user)}\n"
            f"{tg_emoji('gif')} {html.escape(source_label or '—')} · <i>{html.escape(action)}</i>",
        )
        if source_message:
            await copy_render_source_to_owner_chat(context, source_message)


async def is_admin_user(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    user = update.effective_user
    if not user:
        return False

    configured_admins = parse_int_list(os.getenv("ADMIN_USER_IDS"))
    if user.id in configured_admins:
        return True

    target = log_chat_id()
    if not target or not env_bool("ALLOW_LOG_CHAT_ADMINS", True):
        return False

    try:
        member = await context.bot.get_chat_member(target, user.id, read_timeout=20, connect_timeout=20)
    except TelegramError:
        return False
    return member.status in {"creator", "administrator"}


async def require_admin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    if await is_admin_user(update, context):
        return True
    if update.message:
        await update.message.reply_text(t('Нет доступа к админ-командам.'))
    return False


def state_for(user_id: int) -> UserLimitState:
    state = USER_LIMITS.get(user_id)
    if not state:
        state = UserLimitState(render_times=deque(), violation_times=deque())
        USER_LIMITS[user_id] = state
    return state


def load_limit_state() -> None:
    if not LIMITS_STATE_PATH.exists():
        return

    now = time.time()
    try:
        payload = json.loads(LIMITS_STATE_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        logging.exception("Failed to read limit state")
        return

    for raw_user_id, raw_state in payload.get("users", {}).items():
        try:
            user_id = int(raw_user_id)
            banned_until = float(raw_state.get("banned_until", 0))
        except (TypeError, ValueError, AttributeError):
            continue
        if banned_until > now:
            state_for(user_id).banned_until = banned_until


def save_limit_state() -> None:
    now = time.time()
    users = {
        str(user_id): {"banned_until": state.banned_until}
        for user_id, state in USER_LIMITS.items()
        if state.banned_until > now
    }
    payload = {"users": users}
    VAR_DIR.mkdir(parents=True, exist_ok=True)
    temp_path = LIMITS_STATE_PATH.with_suffix(".tmp")
    try:
        temp_path.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")
        temp_path.replace(LIMITS_STATE_PATH)
    except OSError:
        logging.exception("Failed to save limit state")


def load_menu_assets() -> None:
    MENU_ASSETS.clear()
    MENU_SECTION_ASSETS.clear()
    if not MENU_ASSETS_PATH.exists():
        return
    try:
        payload = json.loads(MENU_ASSETS_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        logging.exception("Failed to load menu assets")
        return
    assets = payload.get("animation_file_ids", [])
    if isinstance(assets, list):
        MENU_ASSETS.extend(str(item) for item in assets if item)
    sections = payload.get("sections", {})
    if isinstance(sections, dict):
        for raw_section, raw_assets in sections.items():
            if not isinstance(raw_assets, list):
                continue
            section = normalize_menu_asset_section(str(raw_section))
            MENU_SECTION_ASSETS[section] = [str(item) for item in raw_assets if item]


def save_menu_assets() -> None:
    payload = {
        "animation_file_ids": MENU_ASSETS[-50:],
        "sections": {
            section: assets[-50:]
            for section, assets in sorted(MENU_SECTION_ASSETS.items())
            if section != "main" and assets
        },
    }
    VAR_DIR.mkdir(parents=True, exist_ok=True)
    try:
        MENU_ASSETS_PATH.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    except OSError:
        logging.exception("Failed to save menu assets")


def normalize_menu_asset_section(section: str | None) -> str:
    value = (section or "main").strip().lower()
    return value if value in MENU_ASSET_SECTIONS else "main"


def menu_asset_section_label(section: str) -> str:
    return MENU_ASSET_SECTIONS.get(section, MENU_ASSET_SECTIONS["main"])


def menu_asset_count(section: str) -> int:
    section = normalize_menu_asset_section(section)
    if section == "main":
        return len(MENU_ASSETS)
    return len(MENU_SECTION_ASSETS.get(section, []))


def menu_asset_for(section: str | None = None) -> str | None:
    section = normalize_menu_asset_section(section)
    assets = MENU_SECTION_ASSETS.get(section, []) if section != "main" else MENU_ASSETS
    if assets:
        return secrets.choice(assets)
    if MENU_ASSETS:
        return secrets.choice(MENU_ASSETS)
    return None


async def _generate_section_bg_video(watermark: str, output: Path) -> bool:
    wm_file = output.with_suffix(".wm.txt")
    wm_file.write_text(watermark, encoding="utf-8")
    try:
        run_command([
            "ffmpeg", "-y",
            "-f", "lavfi", "-i",
            f"color=c=black:s=512x288:r=5:d=1.5",
            "-vf",
            f"drawtext=textfile='{wm_file}':fontcolor=white@0.25:fontsize=13:x=w-tw-10:y=h-th-10",
            "-c:v", "libx264", "-pix_fmt", "yuv420p",
            "-t", "1.5",
            str(output),
        ], cwd=ROOT)
        return True
    except Exception:
        logging.exception("Failed to generate bg video")
        return False
    finally:
        if wm_file.exists():
            wm_file.unlink(missing_ok=True)


async def _composite_emoji_onto_video(
    app: Application, emoji_id: str, bg_video: Path, output: Path,
) -> bool:
    try:
        stickers = await app.bot.get_custom_emoji_stickers([emoji_id])
        if not stickers:
            return False
        emoji = stickers[0]
        emoji_file = await app.bot.get_file(emoji.file_id)
        emoji_path = BG_DIR / f"emoji_{emoji_id}"
        await emoji_file.download_to_drive(emoji_path)

        kind = detect_kind(emoji_path)
        frames_dir = BG_DIR / f"emoji_frames_{emoji_id}"
        if kind == "tgs":
            frames_dir.mkdir(parents=True, exist_ok=True)
            run_command([
                sys.executable, str(ROOT / "src" / "render_lottie.py"),
                "--input", str(emoji_path),
                "--out-dir", str(frames_dir),
                "--width", "128", "--height", "128",
                "--fps", "5", "--max-seconds", "1.5",
            ], cwd=ROOT)
            frame_pattern = frames_dir / "frame_%05d.png"
            cmd = [
                "ffmpeg", "-y",
                "-i", str(bg_video),
                "-framerate", "5",
                "-i", str(frame_pattern),
                "-filter_complex",
                f"[1:v]scale=128:128:force_original_aspect_ratio=decrease[emoji];"
                f"[0:v][emoji]overlay=(W-w)/2:(H-h)/2:shortest=1",
                "-c:v", "libx264", "-pix_fmt", "yuv420p",
                "-t", "1.5",
                str(output),
            ]
            run_command(cmd)
            return True
    except Exception:
        logging.exception("Failed to composite emoji %s", emoji_id)
    return False


async def _auto_generate_menu_assets(app: Application) -> None:
    try:
        section_emojis = {
            "bg": ("Цвет фона\nВыбор HEX и загрузка фото", "brush"),
            "resolution": ("Разрешение\n640x360 • 30 FPS", "resolution"),
            "item_color": ("Цвет Emoji\nПерекраска стикеров", "brush"),
            "notes": ("Заметки\nПодпись к результату", "write"),
            "watermark": ("Вотермарка\nТекст поверх видео", "text"),
        }
        wm = os.getenv("WATERMARK_TEXT", "StickerLoop")
        target_chat = log_chat_id()
        admin_ids = parse_int_list(os.getenv("ADMIN_USER_IDS"))
        if not target_chat and not admin_ids:
            return

        for section, (label, emoji_key) in section_emojis.items():
            if MENU_SECTION_ASSETS.get(section):
                continue
            preview_path = BG_DIR / f"section_{section}.mp4"
            final_path = BG_DIR / f"section_{section}_final.mp4"
            BG_DIR.mkdir(parents=True, exist_ok=True)
            if not await _generate_section_bg_video(wm, preview_path):
                continue

            emoji_id = PREMIUM_EMOJI[emoji_key][0]
            if emoji_id:
                if await _composite_emoji_onto_video(app, emoji_id, preview_path, final_path):
                    preview_path = final_path

            chat_id = target_chat or next(iter(admin_ids))
            try:
                with preview_path.open("rb") as f:
                    sent = await app.bot.send_video(
                        chat_id=chat_id,
                        video=f,
                        disable_notification=True,
                    )
                if sent.video and sent.video.file_id:
                    add_menu_asset(sent.video.file_id, section)
                await sent.delete()
            except TelegramError:
                logging.exception("Failed to upload section preview for %s", section)
        save_menu_assets()
        logging.info("Auto-generated menu section previews complete")
    except Exception:
        logging.exception("Menu asset auto-generation skipped (non-critical)")


def add_menu_asset(file_id: str, section: str = "main") -> None:
    section = normalize_menu_asset_section(section)
    assets = MENU_ASSETS if section == "main" else MENU_SECTION_ASSETS.setdefault(section, [])
    if file_id in assets:
        assets.remove(file_id)
    assets.append(file_id)
    save_menu_assets()


def menu_asset_action(section: str = "main") -> str:
    return f"menu_asset:{normalize_menu_asset_section(section)}"


def menu_asset_section_from_action(action: str) -> str:
    if action == "menu_asset":
        return "main"
    if action.startswith("menu_asset:"):
        return normalize_menu_asset_section(action.split(":", 1)[1])
    return "main"


def prune_window(items: deque[float], now: float, window_seconds: int) -> None:
    cutoff = now - window_seconds
    while items and items[0] < cutoff:
        items.popleft()


def format_duration(seconds: float) -> str:
    seconds = max(1, int(seconds))
    if seconds >= 3600:
        hours = seconds // 3600
        minutes = (seconds % 3600) // 60
        return t('{0} ч {1} мин', hours, minutes) if minutes else t('{0} ч', hours)
    if seconds >= 60:
        minutes = seconds // 60
        rest = seconds % 60
        return t('{0} мин {1} сек', minutes, rest) if rest else t('{0} мин', minutes)
    return t('{0} сек', seconds)


def ban_remaining(user_id: int, now: float | None = None) -> float:
    now = now or time.time()
    state = state_for(user_id)
    remaining = state.banned_until - now
    if remaining <= 0:
        if state.banned_until:
            state.banned_until = 0
            save_limit_state()
        return 0
    return remaining


def note_violation(user_id: int, config: SafetyConfig, now: float | None = None) -> float:
    now = now or time.time()
    state = state_for(user_id)
    prune_window(state.violation_times, now, config.spam_window_seconds)
    state.violation_times.append(now)
    if len(state.violation_times) >= config.spam_events_before_ban:
        state.violation_times.clear()
        state.banned_until = now + config.ban_seconds
        save_limit_state()
        logging.warning("User %s temporarily banned for spam until %.0f", user_id, state.banned_until)
        return config.ban_seconds
    return 0


def user_rate_delay(user_id: int, config: SafetyConfig, now: float | None = None) -> float:
    now = now or time.time()
    state = state_for(user_id)
    prune_window(state.render_times, now, config.per_user_window_seconds)

    if state.render_times:
        since_last = now - state.render_times[-1]
        if since_last < config.per_user_min_gap_seconds:
            return config.per_user_min_gap_seconds - since_last

    if len(state.render_times) >= config.per_user_window_jobs:
        return config.per_user_window_seconds - (now - state.render_times[0])

    return 0


def mark_render_start(user_id: int, config: SafetyConfig, now: float | None = None) -> None:
    now = now or time.time()
    state = state_for(user_id)
    prune_window(state.render_times, now, config.per_user_window_seconds)
    state.render_times.append(now)


def mark_render_in_db(user_id: int) -> None:
    with db_connect() as conn:
        conn.execute(
            """
            UPDATE users
            SET render_count = render_count + 1,
                last_seen = ?,
                last_action = 'render'
            WHERE user_id = ?
            """,
            (int(time.time()), user_id),
        )


def get_render_gate() -> RenderGate:
    global GLOBAL_RENDER_GATE
    if GLOBAL_RENDER_GATE is None:
        GLOBAL_RENDER_GATE = RenderGate(safety_config().max_global_renders)
    return GLOBAL_RENDER_GATE


def cleanup_old_runs(config: SafetyConfig) -> None:
    if not RUNS_DIR.exists():
        return

    now = time.time()
    for child in RUNS_DIR.iterdir():
        try:
            if not child.is_dir():
                continue
            age = now - child.stat().st_mtime
            if age > config.runs_retention_seconds:
                shutil.rmtree(child, ignore_errors=True)
        except OSError:
            logging.exception("Failed to clean old run directory %s", child)


def background_keyboard() -> InlineKeyboardMarkup:
    rows = []
    items = list(BACKGROUND_PRESETS.items())
    for index in range(0, len(items), 2):
        row = [
            menu_button(f"{t(name)} {color}", f"bg:{key}", "brush")
            for key, (name, color) in items[index:index + 2]
        ]
        rows.append(row)
    return InlineKeyboardMarkup(rows)


def output_format_label(value: str) -> str:
    return {
        "gif": "GIF",
        "video": t('Видео'),
        "file": t('Файл'),
    }.get(value, "GIF")


# Telegram iOS Icons: https://emoji.wivvi.net
PREMIUM_EMOJI = {
    'settings': ('6032742198179532882', '⚙'),
    'file': ('6037475557082403885', '📁'),
    'send': ('6039391666547201160', '⬆️'),
    'brush': ('6050679691004612757', '🖌'),
    'media': ('6030466823290360017', '🖼'),
    'resolution': ('5778479949572738874', '↔️'),
    'text': ('5771851822897566479', '🔡'),
    'write': ('6039614175917903752', '✏'),
    'eye': ('6037397706505195857', '👁'),
    'delete': ('6039522349517115015', '🗑'),
    'check': ('5774022692642492953', '✅'),
    'info': ('6028435952299413210', 'ℹ'),
    'bot': ('6030400221232501136', '🤖'),
    'loading': ('6030657343744644592', '🔁'),
    'back': ('5960671702059848143', '⬅️'),
    'globe': ('5776233299424843260', '🌐'),
    'heart': ('5938368005611195877', '❤️'),
    'stars': ('6028338546736107668', '⭐️'),
    'gif': ('5944777041709633960', '🎞'),
    'warning': ('6030563507299160824', '❗️'),
    'error': ('5774077015388852135', '❌'),
    'stats': ('5936143551854285132', '📊'),
    'users': ('6032609071373226027', '👥'),
    'message': ('6030784887093464891', '💬'),
    'calendar': ('5890937706803894250', '📅'),
    'new': ('5895669571058142797', '🆕'),
    'active': ('5884428842780594914', '⚡'),
    'box': ('5884479287171485878', '📦'),
    'help': ('6030848053177486888', '❓'),
    'tap': ('5886583490434044162', '👆'),
}


def tg_emoji(key: str) -> str:
    emoji_id, fallback = PREMIUM_EMOJI[key]
    return f'<tg-emoji emoji-id="{emoji_id}">{fallback}</tg-emoji>'



def menu_button(text: str, callback_data: str, icon: str = "back") -> InlineKeyboardButton:
    return InlineKeyboardButton(text, callback_data=callback_data, icon_custom_emoji_id=PREMIUM_EMOJI[icon][0])



def menu_surface(message: Message) -> str:
    return "caption" if message.caption is not None else "text"


def pending_from_message(action: str, message: Message) -> PendingAction:
    return PendingAction(
        action=action,
        chat_id=message.chat_id,
        message_id=message.message_id,
        surface=menu_surface(message),
    )


def _bg_value_label(settings: RenderSettings) -> str:
    if settings.gradient_end_hex:
        return f"{settings.background_hex} → {settings.gradient_end_hex}"
    return settings.background_hex


def settings_summary(settings: RenderSettings) -> str:
    item_color = html.escape(settings.item_color_hex or t('выкл'))
    notes = html.escape(settings.notes[:40] if settings.notes else t('нет'))
    watermark = html.escape(settings.watermark_text if settings.watermark_enabled and settings.watermark_text else t('выкл'))
    return (
        t(
            '{0} <b>Настройки рендера</b>\n'
            '\n'
            '<blockquote>{1} <b>Фон:</b> {2}\n'
            '{3} <b>Размер:</b> {4}×{5} · {6} FPS\n'
            '{7} <b>Формат:</b> {8}\n'
            '{9} <b>Перекраска:</b> {10}\n'
            '{11} <b>Подпись:</b> {12}\n'
            '{13} <b>Вотермарка:</b> {14}</blockquote>\n'
            '\n'
            'Кнопки показывают текущее значение — жми, чтобы поменять {15}',
            tg_emoji('settings'),
            tg_emoji('brush'),
            _bg_value_label(settings),
            tg_emoji('resolution'),
            settings.width,
            settings.height,
            settings.fps,
            tg_emoji('file'),
            output_format_label(settings.output_format),
            tg_emoji('brush'),
            item_color,
            tg_emoji('write'),
            notes,
            tg_emoji('text'),
            watermark,
            tg_emoji('tap'),
        )
    )


def main_menu_keyboard(settings: RenderSettings) -> InlineKeyboardMarkup:
    bg_val = t('градиент') if settings.gradient_end_hex else settings.background_hex
    color_val = settings.item_color_hex or t('выкл')
    notes_val = t('есть') if settings.notes else t('нет')
    wm_val = t('вкл') if settings.watermark_enabled else t('выкл')
    return InlineKeyboardMarkup(
        [
            [
                menu_button(t('Фон · {0}', bg_val), "menu:bg", "brush"),
                menu_button(t('Размер · {0}×{1}', settings.width, settings.height), "menu:resolution", "resolution"),
            ],
            [
                menu_button(t('Перекраска · {0}', color_val), "menu:item_color", "brush"),
            ],
            [
                menu_button(t('Подпись · {0}', notes_val), "menu:notes", "write"),
                menu_button(t('Вотермарка · {0}', wm_val), "menu:watermark", "text"),
            ],
            [
                menu_button(t('Сбросить всё'), "menu:reset", "delete"),
            ],
            [menu_button(t("Страна / язык"), "lang:menu:settings", "globe")],
        ]
    )


def back_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[menu_button(t('Назад'), "menu:main")]])


def background_menu_keyboard(user_id: int) -> InlineKeyboardMarkup:
    has_custom = user_id in USER_BG_IMAGES
    current_settings = settings_for(user_id)
    rows = []
    items = list(BACKGROUND_PRESETS.items())
    for index in range(0, len(items), 2):
        rows.append(
            [
                menu_button(f"{t(name)} {color}", f"setbg:{key}", "brush")
                for key, (name, color) in items[index:index + 2]
            ]
        )
    if has_custom:
        rows.append([menu_button(t('Сбросить на цвет'), f"setbgimg:reset:{current_settings.background_key}", "delete")])
    rows.append([menu_button(t('Градиент'), "menu:gradient", "brush")])
    rows.append([menu_button(t('Загрузить свой фон'), "menu:bg_upload", "media")])
    rows.append([menu_button(t('Назад'), "menu:main")])
    return InlineKeyboardMarkup(rows)


GRADIENT_PREVIEW_CACHE: dict[str, str] = {}


def _render_gradient_preview(c0_hex: str, c1_hex: str, direction: str, output: Path) -> bool:
    w, h = (640, 60)
    c0 = c0_hex.lstrip("#")
    c1 = c1_hex.lstrip("#")
    dr_r = int(c1[0:2], 16) - int(c0[0:2], 16)
    dr_g = int(c1[2:4], 16) - int(c0[2:4], 16)
    dr_b = int(c1[4:6], 16) - int(c0[4:6], 16)
    axis = "Y" if direction == "v" else "X"
    dim = "H" if direction == "v" else "W"
    geq = (
        f"geq=r='r({axis},Y)+floor({dr_r}*{axis}/{dim})':"
        f"g='g({axis},Y)+floor({dr_g}*{axis}/{dim})':"
        f"b='b({axis},Y)+floor({dr_b}*{axis}/{dim})'"
    )
    cmd = [
        "ffmpeg", "-y",
        "-f", "lavfi", "-i", f"color=c={c0_hex}:s={w}x{h}:r=1:d=0.1",
        "-vf", geq,
        "-frames:v", "1",
        "-c:v", "mjpeg",
        str(output),
    ]
    try:
        run_command(cmd, cwd=ROOT)
        return True
    except Exception:
        return False


async def _send_gradient_preview(
    context: ContextTypes.DEFAULT_TYPE,
    chat_id: int,
    current: RenderSettings,
    message_id: int | None = None,
) -> None:
    c0 = current.background_hex
    c1 = current.gradient_end_hex
    direction = current.gradient_direction or "h"

    if not c1:
        _, c0, c1, direction = GRADIENT_PRESETS[0]

    cache_key = f"{c0}/{c1}/{direction}"

    if cache_key not in GRADIENT_PREVIEW_CACHE:
        preview_path = BG_DIR / f"gp_{abs(hash(cache_key))}.jpg"
        BG_DIR.mkdir(parents=True, exist_ok=True)
        if _render_gradient_preview(c0, c1, direction, preview_path):
            try:
                sent = await context.bot.send_photo(
                    chat_id=chat_id,
                    photo=preview_path.open("rb"),
                    disable_notification=True,
                )
                p = sent.photo
                if p and p[-1]:
                    GRADIENT_PREVIEW_CACHE[cache_key] = p[-1].file_id
                await sent.delete()
            except TelegramError:
                pass

    preview_id = GRADIENT_PREVIEW_CACHE.get(cache_key)
    dir_label = t('↕ вертикаль') if direction == "v" else t('↔ горизонталь')
    caption = t('{0} <b>Градиент:</b> {1} → {2} ({3})', tg_emoji('brush'), c0, c1, dir_label)

    if preview_id and message_id:
        try:
            await context.bot.edit_message_media(
                chat_id=chat_id,
                message_id=message_id,
                media=InputMediaPhoto(media=preview_id, caption=caption, parse_mode=ParseMode.HTML),
                reply_markup=gradient_menu_keyboard(current),
            )
            return
        except (BadRequest, TelegramError):
            pass

    if preview_id:
        await context.bot.send_photo(
            chat_id=chat_id,
            photo=preview_id,
            caption=caption,
            reply_markup=gradient_menu_keyboard(current),
            parse_mode=ParseMode.HTML,
        )
    else:
        await context.bot.send_message(
            chat_id=chat_id,
            text=t('{0} <b>Градиент:</b> {1} → {2} ({3})\n\n{4} <b>Выбери градиент:</b>', tg_emoji('brush'), c0, c1, dir_label, tg_emoji('brush')),
            reply_markup=gradient_menu_keyboard(current),
            parse_mode=ParseMode.HTML,
        )


def gradient_menu_keyboard(current: RenderSettings) -> InlineKeyboardMarkup:
    dir_label = {"h": t('↔ горизонталь'), "v": t('↕ вертикаль')}
    rows = []
    for name, c0, c1, direction in GRADIENT_PRESETS:
        active = current.gradient_end_hex == c1 and current.background_hex == c0
        prefix = "✓ " if active else ""
        rows.append([menu_button(f"{prefix}{t(name)} {dir_label.get(direction, '')}", f"setgradient:{direction}/{c0}/{c1}", "brush")])
    rows.append([menu_button(t('Назад'), "menu:bg")])
    return InlineKeyboardMarkup(rows)


def resolution_menu_keyboard(current: RenderSettings) -> InlineKeyboardMarkup:
    def label(key: str) -> str:
        w, h, fps = RESOLUTION_PRESETS[key]
        mark = "✓ " if current.width == w and current.height == h and current.fps == fps else ""
        return f"{mark}{key}"

    rows = []
    items = list(RESOLUTION_PRESETS.items())
    for index in range(0, len(items), 2):
        rows.append(
            [
                menu_button(label(key), f"setres:{key}", "resolution")
                for key, _ in items[index:index + 2]
            ]
        )
    rows.append([menu_button(t('Свой размер…'), "menu:res_custom", "resolution")])
    rows.append([menu_button(t('Назад'), "menu:main")])
    return InlineKeyboardMarkup(rows)



def item_color_keyboard(current_hex: str | None) -> InlineKeyboardMarkup:
    def label(key: str) -> str:
        name, hex_color = ITEM_COLOR_PRESETS[key]
        mark = "✓ " if current_hex == hex_color else ""
        return f"{mark}{t(name)}"

    rows = []
    items = list(ITEM_COLOR_PRESETS.items())
    for index in range(0, len(items), 2):
        rows.append(
            [
                menu_button(label(key), f"setcolor:{key}", "brush")
                for key, _ in items[index:index + 2]
            ]
        )
    rows.append([menu_button(t('Свой цвет…'), "menu:item_color_custom", "brush")])
    if current_hex:
        rows.append([menu_button(t('Без цвета'), "itemcolor:clear", "delete")])
    rows.append([menu_button(t('Назад'), "menu:main")])
    return InlineKeyboardMarkup(rows)



def notes_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [menu_button(t('Убрать заметки'), "notes:clear", "delete")],
            [menu_button(t('Назад'), "menu:main")],
        ]
    )


def watermark_keyboard(settings: RenderSettings) -> InlineKeyboardMarkup:
    toggle = t('✓ Включена') if settings.watermark_enabled else t('Включить')
    return InlineKeyboardMarkup(
        [
            [menu_button(toggle, "wm:toggle", "check")],
            [menu_button(t('Текст вотермарки'), "wm:text", "text")],
            [menu_button(t('Назад'), "menu:main")],
        ]
    )


async def safe_delete_message(message: Message) -> None:
    try:
        await message.delete()
    except TelegramError:
        pass


async def edit_menu_message(message: Message, text: str, reply_markup: InlineKeyboardMarkup) -> Message | None:
    try:
        if message.caption is not None:
            sent = await message.reply_text(
                text,
                parse_mode=ParseMode.HTML,
                reply_markup=reply_markup,
                disable_web_page_preview=True,
            )
            try:
                await message.edit_reply_markup(reply_markup=None)
            except TelegramError:
                logging.exception("Failed to remove old media menu keyboard")
            return sent
        return await message.edit_text(
            text=text,
            parse_mode=ParseMode.HTML,
            reply_markup=reply_markup,
            disable_web_page_preview=True,
        )
    except BadRequest as error:
        if "message is not modified" in str(error).lower():
            return message
        logging.warning("Failed to edit menu message: %s", error)
    except TelegramError:
        logging.exception("Failed to edit menu message")
    return None


async def edit_pending_menu(
    context: ContextTypes.DEFAULT_TYPE,
    pending: PendingAction,
    text: str,
    reply_markup: InlineKeyboardMarkup,
) -> None:
    try:
        if pending.surface == "caption":
            await context.bot.edit_message_caption(
                chat_id=pending.chat_id,
                message_id=pending.message_id,
                caption=text,
                parse_mode=ParseMode.HTML,
                reply_markup=reply_markup,
            )
            return
        await context.bot.edit_message_text(
            chat_id=pending.chat_id,
            message_id=pending.message_id,
            text=text,
            parse_mode=ParseMode.HTML,
            reply_markup=reply_markup,
            disable_web_page_preview=True,
        )
    except BadRequest as error:
        if "message is not modified" not in str(error).lower():
            logging.warning("Failed to edit pending menu message: %s", error)
    except TelegramError:
        logging.exception("Failed to edit pending menu message")


async def send_menu_message(message: Message, settings: RenderSettings, section: str = "main") -> Message:
    return await message.reply_text(
        settings_summary(settings),
        parse_mode=ParseMode.HTML,
        reply_markup=main_menu_keyboard(settings),
        disable_web_page_preview=True,
    )


async def show_section_menu_message(
    context: ContextTypes.DEFAULT_TYPE,
    message: Message,
    text: str,
    reply_markup: InlineKeyboardMarkup,
    section: str,
) -> Message | None:
    return await edit_menu_message(message, text, reply_markup)


def source_from_sticker(sticker: Sticker) -> SourceRef:
    premium_animation = getattr(sticker, "premium_animation", None)
    if sticker.is_animated:
        return SourceRef(sticker.file_id, "animated .tgs sticker")
    if sticker.is_video:
        return SourceRef(sticker.file_id, "video .webm sticker")
    if premium_animation and getattr(premium_animation, "file_id", None):
        return SourceRef(premium_animation.file_id, "premium animation")
    return SourceRef(sticker.file_id, "static sticker")


def custom_emoji_ids(message: Message) -> list[str]:
    entities = list(message.entities or []) + list(message.caption_entities or [])
    ids = []
    for entity in entities:
        if entity.type == "custom_emoji" and entity.custom_emoji_id:
            ids.append(entity.custom_emoji_id)
    return ids


def detect_kind(path: Path) -> str:
    suffix = path.suffix.lower()
    head = path.read_bytes()[:16]
    if suffix == ".tgs" or head.startswith(b"\x1f\x8b"):
        return "tgs"
    if suffix == ".webm" or head.startswith(b"\x1a\x45\xdf\xa3"):
        return "webm"
    if suffix in {".webp", ".png", ".jpg", ".jpeg"}:
        return "image"
    if suffix in {".mp4", ".mov", ".m4v", ".gif"}:
        return "video"
    return "unknown"


def ffprobe_duration(path: Path) -> float | None:
    cmd = [
        "ffprobe",
        "-v",
        "error",
        "-show_entries",
        "format=duration",
        "-of",
        "default=noprint_wrappers=1:nokey=1",
        str(path),
    ]
    try:
        result = subprocess.run(cmd, check=True, capture_output=True, text=True, timeout=15)
        duration = float(result.stdout.strip())
        if duration > 0:
            return min(duration, 6.0)
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, ValueError):
        return None
    return None


def run_command(cmd: list[str], cwd: Path | None = None) -> None:
    try:
        result = subprocess.run(
            cmd,
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=safety_config().render_timeout_seconds,
        )
    except subprocess.TimeoutExpired as error:
        raise RuntimeError(f"Render command timed out after {error.timeout:.0f}s") from error

    if result.returncode != 0:
        message = result.stderr.strip() or result.stdout.strip() or "command failed"
        raise RuntimeError(message[-4000:])


def ffmpeg_common_output(output: Path) -> list[str]:
    return [
        "-map",
        "[v]",
        "-an",
        "-c:v",
        "libx264",
        "-preset",
        "veryfast",
        "-crf",
        "18",
        "-pix_fmt",
        "yuv420p",
        "-movflags",
        "+faststart",
        str(output),
    ]


def drawtext_escape(value: str) -> str:
    return (
        value
        .replace("\\", "\\\\")
        .replace(":", "\\:")
        .replace("'", "\\'")
        .replace("%", "\\%")
        .replace(",", "\\,")
        .replace("[", "\\[")
        .replace("]", "\\]")
    )


def has_ffmpeg_drawtext() -> bool:
    global HAS_DRAWTEXT_FILTER
    if HAS_DRAWTEXT_FILTER is not None:
        return HAS_DRAWTEXT_FILTER

    try:
        result = subprocess.run(
            ["ffmpeg", "-hide_banner", "-filters"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        HAS_DRAWTEXT_FILTER = " drawtext " in result.stdout
    except (OSError, subprocess.TimeoutExpired):
        HAS_DRAWTEXT_FILTER = False

    if not HAS_DRAWTEXT_FILTER:
        logging.warning("ffmpeg drawtext filter is unavailable; watermark disabled")
    return HAS_DRAWTEXT_FILTER


def watermark_drawtext_filter(settings: RenderSettings) -> str:
    if not settings.watermark_enabled:
        return ""
    if not has_ffmpeg_drawtext():
        return ""

    text = settings.watermark_text.strip()
    if not text:
        return ""

    opacity = clamp(env_float("WATERMARK_OPACITY", 0.16), 0.0, 1.0)
    shadow_opacity = clamp(env_float("WATERMARK_SHADOW_OPACITY", 0.12), 0.0, 1.0)
    font_size = max(8, env_int("WATERMARK_FONT_SIZE", 13))
    margin = max(0, env_int("WATERMARK_MARGIN", 12))
    font_color = env_str("WATERMARK_COLOR", "white")
    shadow_color = env_str("WATERMARK_SHADOW_COLOR", "black")
    position = env_str("WATERMARK_POSITION", "bottom_left").strip().lower()
    if position == "bottom_left":
        x_expr = str(margin)
        y_expr = f"h-th-{margin}"
    elif position == "top_left":
        x_expr = str(margin)
        y_expr = str(margin)
    elif position == "top_right":
        x_expr = f"w-tw-{margin}"
        y_expr = str(margin)
    else:
        x_expr = f"w-tw-{margin}"
        y_expr = f"h-th-{margin}"
    escaped_text = drawtext_escape(text)

    return (
        f",drawtext=text='{escaped_text}':"
        f"fontcolor={font_color}@{opacity:.3f}:"
        f"fontsize={font_size}:"
        f"x={x_expr}:"
        f"y={y_expr}:"
        f"shadowcolor={shadow_color}@{shadow_opacity:.3f}:"
        "shadowx=1:shadowy=1"
    )


def sticker_filter(settings: RenderSettings) -> str:
    base = (
        f"fps={settings.fps},"
        f"scale={settings.sticker_size}:{settings.sticker_size}:"
        "force_original_aspect_ratio=decrease:flags=lanczos,"
        "format=rgba"
    )
    if settings.item_color_hex:
        red = int(settings.item_color_hex[1:3], 16)
        green = int(settings.item_color_hex[3:5], 16)
        blue = int(settings.item_color_hex[5:7], 16)
        base += f",geq=r={red}:g={green}:b={blue}:a=alpha(X\\,Y)"
    return base


def compose_filter(settings: RenderSettings, image_bg: bool = False, gradient_vf: str = "") -> str:
    watermark = watermark_drawtext_filter(settings)
    bg_chain = (
        f"[1:v]scale={settings.width}:{settings.height}:force_original_aspect_ratio=increase,"
        f"crop={settings.width}:{settings.height},fps={settings.fps}[bg];"
        if image_bg
        else ""
    )
    if gradient_vf and not image_bg:
        bg_chain = f"[1:v]{gradient_vf}[bg];"
    bg_label = "bg" if (image_bg or gradient_vf) else "1:v"
    return (
        f"{bg_chain}"
        f"[0:v]{sticker_filter(settings)}[st];"
        f"[{bg_label}][st]overlay=(W-w)/2:(H-h)/2:shortest=1:format=auto,"
        f"format=yuv420p{watermark}[v]"
    )


def _make_bg_args(settings: RenderSettings, user_id: int, duration: float) -> tuple[list[str], bool, str]:
    """Returns (ffmpeg_args, is_image_bg, extra_vf)"""
    bg_path = USER_BG_IMAGES.get(user_id)
    if bg_path and bg_path.exists():
        return (["-loop", "1", "-i", str(bg_path)], True, "")

    if settings.gradient_end_hex:
        c0 = settings.background_hex.lstrip("#")
        c1 = settings.gradient_end_hex.lstrip("#")
        dr_r = int(c1[0:2], 16) - int(c0[0:2], 16)
        dr_g = int(c1[2:4], 16) - int(c0[2:4], 16)
        dr_b = int(c1[4:6], 16) - int(c0[4:6], 16)
        axis = "Y" if settings.gradient_direction == "v" else "X"
        dim = "H" if settings.gradient_direction == "v" else "W"
        geq = (
            f"geq=r='r({axis},Y)+floor({dr_r}*{axis}/{dim})':"
            f"g='g({axis},Y)+floor({dr_g}*{axis}/{dim})':"
            f"b='b({axis},Y)+floor({dr_b}*{axis}/{dim})'"
        )
        color = f"color=c={settings.background_hex}:s={settings.width}x{settings.height}:r={settings.fps}:d={duration}"
        return (["-f", "lavfi", "-i", color], False, geq)

    color = f"color=c={settings.background_hex}:s={settings.width}x{settings.height}:r={settings.fps}:d={duration}"
    return (["-f", "lavfi", "-i", color], False, "")


def render_tgs(source: Path, output: Path, job_dir: Path, settings: RenderSettings, user_id: int) -> None:
    frames_dir = job_dir / "frames"
    render_cmd = [
        sys.executable,
        str(ROOT / "src" / "render_lottie.py"),
        "--input",
        str(source),
        "--out-dir",
        str(frames_dir),
        "--width",
        "512",
        "--height",
        "512",
        "--fps",
        str(settings.fps),
        "--max-seconds",
        "6",
    ]
    run_command(render_cmd, cwd=ROOT)

    manifest = json.loads((frames_dir / "manifest.json").read_text(encoding="utf-8"))
    duration = max(0.2, min(float(manifest["duration"]), 6.0))
    frame_pattern = frames_dir / "frame_%05d.png"

    bg_args, image_bg, gradient_vf = _make_bg_args(settings, user_id, duration)

    cmd = [
        "ffmpeg",
        "-y",
        "-framerate",
        str(settings.fps),
        "-i",
        str(frame_pattern),
        *bg_args,
        "-filter_complex",
        compose_filter(settings, image_bg=image_bg, gradient_vf=gradient_vf),
        "-t",
        f"{duration:.3f}",
        *ffmpeg_common_output(output),
    ]
    run_command(cmd)


def render_webm(source: Path, output: Path, settings: RenderSettings, user_id: int) -> None:
    duration = ffprobe_duration(source) or 3.0
    bg_args, image_bg, gradient_vf = _make_bg_args(settings, user_id, duration)
    cmd = [
        "ffmpeg",
        "-y",
        "-stream_loop",
        "-1",
        "-t",
        f"{duration:.3f}",
        "-i",
        str(source),
        *bg_args,
        "-filter_complex",
        compose_filter(settings, image_bg=image_bg, gradient_vf=gradient_vf),
        "-t",
        f"{duration:.3f}",
        *ffmpeg_common_output(output),
    ]
    run_command(cmd)


def render_video(source: Path, output: Path, settings: RenderSettings, user_id: int) -> None:
    duration = ffprobe_duration(source) or 3.0
    bg_args, image_bg, gradient_vf = _make_bg_args(settings, user_id, duration)
    cmd = [
        "ffmpeg",
        "-y",
        "-stream_loop",
        "-1",
        "-t",
        f"{duration:.3f}",
        "-i",
        str(source),
        *bg_args,
        "-filter_complex",
        compose_filter(settings, image_bg=image_bg, gradient_vf=gradient_vf),
        "-t",
        f"{duration:.3f}",
        *ffmpeg_common_output(output),
    ]
    run_command(cmd)


def render_image(source: Path, output: Path, settings: RenderSettings, user_id: int) -> None:
    duration = max(0.5, min(settings.static_seconds, 6.0))
    bg_args, image_bg, gradient_vf = _make_bg_args(settings, user_id, duration)
    cmd = [
        "ffmpeg",
        "-y",
        "-loop",
        "1",
        "-t",
        f"{duration:.3f}",
        "-i",
        str(source),
        *bg_args,
        "-filter_complex",
        compose_filter(settings, image_bg=image_bg, gradient_vf=gradient_vf),
        "-t",
        f"{duration:.3f}",
        *ffmpeg_common_output(output),
    ]
    run_command(cmd)


def render_source(source: Path, job_dir: Path, settings: RenderSettings, user_id: int) -> Path:
    kind = detect_kind(source)
    output = job_dir / "loop.mp4"
    if kind == "tgs":
        render_tgs(source, output, job_dir, settings, user_id)
    elif kind == "webm":
        render_webm(source, output, settings, user_id)
    elif kind == "image":
        render_image(source, output, settings, user_id)
    elif kind == "video":
        render_video(source, output, settings, user_id)
    else:
        raise RuntimeError("Unsupported sticker file format from Telegram")

    if not output.exists() or output.stat().st_size == 0:
        raise RuntimeError("Renderer produced an empty output file")
    if output.stat().st_size > safety_config().max_output_bytes:
        raise UserFacingError(t('Результат получился слишком большим для отправки. Попробуй другой стикер.'))
    return output


async def download_source(context: ContextTypes.DEFAULT_TYPE, source: SourceRef, job_dir: Path) -> Path:
    tg_file = await context.bot.get_file(source.file_id, read_timeout=30, connect_timeout=30)
    file_size = getattr(tg_file, "file_size", None)
    if file_size and file_size > safety_config().max_source_bytes:
        raise UserFacingError(t('Файл стикера слишком большой для безопасной обработки.'))

    suffix = Path(tg_file.file_path or "").suffix or ".bin"
    local_path = job_dir / f"source{suffix}"
    await tg_file.download_to_drive(custom_path=local_path, read_timeout=60, write_timeout=60)
    if local_path.stat().st_size > safety_config().max_source_bytes:
        raise UserFacingError(t('Файл стикера слишком большой для безопасной обработки.'))
    return local_path


async def reply_ban_or_warning(
    message: Message,
    user_id: int,
    config: SafetyConfig,
    warning: str,
) -> None:
    ban_seconds = note_violation(user_id, config)
    if ban_seconds:
        await message.reply_text(
            t('Слишком много попыток подряд. Ставлю паузу на {0}.', format_duration(ban_seconds))
        )
        return
    await message.reply_text(warning)


async def process_source(
    message: Message,
    context: ContextTypes.DEFAULT_TYPE,
    source: SourceRef,
    settings: RenderSettings,
    actor_user_id: int | None = None,
    charge_rate: bool = True,
    edit_target: ResultRef | None = None,
    refresh_revision: int | None = None,
) -> Message | None:
    config = safety_config()
    user_id = actor_user_id or (message.from_user.id if message.from_user else message.chat_id)
    remaining = ban_remaining(user_id)
    if remaining:
        if edit_target:
            return None
        await message.reply_text(t('Пауза после спама еще {0}.', format_duration(remaining)))
        return

    if user_id in BUSY:
        if edit_target:
            return None
        await reply_ban_or_warning(
            message,
            user_id,
            config,
            t('Уже собираю предыдущую анимацию. Дождись результата; повторные стикеры подряд считаются спамом.'),
        )
        return

    delay = user_rate_delay(user_id, config) if charge_rate else 0
    if delay > 0:
        await reply_ban_or_warning(
            message,
            user_id,
            config,
            (
                t(
                    'Лимит: не больше {0} рендеров за {1}. Попробуй через {2}.',
                    config.per_user_window_jobs,
                    format_duration(config.per_user_window_seconds),
                    format_duration(delay),
                )
            ),
        )
        return

    gate = get_render_gate()
    BUSY.add(user_id)
    if not await gate.try_acquire():
        BUSY.discard(user_id)
        if edit_target:
            return None
        await reply_ban_or_warning(
            message,
            user_id,
            config,
            t('Сейчас заняты все {0} слота рендера. Попробуй чуть позже.', config.max_global_renders),
        )
        return

    BUSY.add(user_id)
    if charge_rate:
        mark_render_start(user_id, config)
    if not edit_target:
        await asyncio.to_thread(mark_render_in_db, user_id)
    started = time.time()
    job_dir = Path(tempfile.mkdtemp(prefix="job-", dir=RUNS_DIR))
    sent_message: Message | None = None
    loading_shown = False
    try:
        if edit_target:
            if REFRESH_REVISIONS.get(user_id) != refresh_revision:
                return None
            if not edit_target.file_id:
                previous = await context.bot.edit_message_caption(
                    chat_id=edit_target.chat_id, message_id=edit_target.message_id,
                    caption=f"{tg_emoji('loading')} {html.escape(t('Генерируется…'))}", parse_mode=ParseMode.HTML,
                )
                media = previous.animation or previous.video or previous.document
                previous_format = "gif" if previous.animation else "video" if previous.video else "file"
                edit_target = replace(edit_target, file_id=media.file_id, output_format=previous_format)
                LAST_RESULT[user_id] = edit_target
                loading_shown = True
                await asyncio.to_thread(save_render_session, user_id)
            try:
                with (ROOT / "assets/loading.gif").open("rb") as loading_file:
                    await context.bot.edit_message_media(
                        chat_id=edit_target.chat_id, message_id=edit_target.message_id,
                        media=InputMediaAnimation(media=loading_file, caption=f"{tg_emoji('loading')} {html.escape(t('Генерируется…'))}", parse_mode=ParseMode.HTML, filename="loading.gif"),
                        read_timeout=60, write_timeout=120, connect_timeout=30, pool_timeout=60,
                    )
            except BadRequest as error:
                if "message is not modified" not in str(error).lower():
                    raise
            loading_shown = True
            logging.info("Showing loading animation for message %s, background %s", edit_target.message_id, settings.background_hex)
        await context.bot.send_chat_action(chat_id=message.chat_id, action=ChatAction.UPLOAD_VIDEO)
        source_path = await download_source(context, source, job_dir)
        output = await asyncio.to_thread(render_source, source_path, job_dir, settings, user_id)
        elapsed = time.time() - started
        if not edit_target and env_bool("LOG_RENDER_REQUESTS", True) and message.from_user:
            size_mb = output.stat().st_size / 1_000_000
            await log_to_owner_chat(
                context,
                f"{tg_emoji('check')} <b>Рендер готов</b> · {elapsed:.1f}s\n"
                f"{user_html(message.from_user)}\n"
                f"{tg_emoji('gif')} {html.escape(source.label)} → {output_format_label(settings.output_format)} "
                f"{settings.width}×{settings.height}\n"
                f"{tg_emoji('brush')} {_bg_value_label(settings)} · {tg_emoji('box')} {size_mb:.2f} MB",
            )
        caption = (
            t(
                'Готово: {0}x{1}, {2}, {3}, {4}s',
                settings.width,
                settings.height,
                settings.background_hex,
                output_format_label(settings.output_format),
                format(elapsed, '.1f'),
            )
        )
        caption = f"{tg_emoji('check')} {html.escape(caption)}"
        if settings.notes:
            caption = f"{caption}\n{html.escape(settings.notes[:800])}"
        with output.open("rb") as file_obj:
            if edit_target:
                if (REFRESH_REVISIONS.get(user_id) != refresh_revision
                        or LAST_RESULT.get(user_id) != edit_target):
                    return None
                sent_message = await context.bot.edit_message_media(
                    chat_id=edit_target.chat_id,
                    message_id=edit_target.message_id,
                    media=InputMediaAnimation(media=file_obj, caption=caption, filename="sticker-loop.mp4", parse_mode=ParseMode.HTML),
                    read_timeout=60, write_timeout=120, connect_timeout=30, pool_timeout=60,
                )
                logging.info("Updated existing render message in place")
            else:
                sent_message = await message.reply_animation(
                    animation=file_obj,
                    caption=caption,
                    parse_mode=ParseMode.HTML,
                    reply_markup=main_menu_keyboard(settings),
                    read_timeout=60,
                    write_timeout=120,
                    connect_timeout=30,
                    pool_timeout=60,
                )
        if sent_message:
            media = sent_message.animation or sent_message.video or sent_message.document
            LAST_RESULT[user_id] = ResultRef(
                source, sent_message.chat_id, sent_message.message_id, media.file_id, settings.output_format, caption, True,
            )
            if not edit_target:
                LAST_SOURCE[user_id] = source
                MISSING_RESULT_NOTICE.discard(user_id)
            await asyncio.to_thread(save_render_session, user_id)
    except UserFacingError as error:
        await message.reply_text(str(error))
        if env_bool("LOG_RENDER_REQUESTS", True):
            await log_to_owner_chat(
                context,
                f"{tg_emoji('warning')} <b>Рендер отклонён</b>\n"
                f"{user_html(message.from_user) if message.from_user else f'<code>{user_id}</code>'}\n"
                f"{tg_emoji('gif')} {html.escape(source.label)}\n"
                f"{tg_emoji('message')} {html.escape(str(error))}",
            )
    except BadRequest as error:
        if edit_target and "message is not modified" in str(error).lower():
            return None
        if edit_target and any(part in str(error).lower() for part in (
            "message to edit not found", "message can't be edited", "message_id_invalid",
        )):
            LAST_RESULT.pop(user_id, None)
            await asyncio.to_thread(save_render_session, user_id)
            await message.reply_text(t("Пришли исходник ещё раз — дальше буду обновлять этот результат при смене настроек."))
            return None
        logging.exception("Telegram rejected a rendered result")
        await message.reply_text(t('Не смог собрать анимацию. Пришли другой стикер или попробуй фон попроще.\nТехнически: {0}', str(error)[-900:]))
    except Exception as error:  # noqa: BLE001 - bot replies need a compact user-facing error.
        logging.exception("Failed to process %s", source.label)
        await message.reply_text(
            t('Не смог собрать анимацию. Пришли другой стикер или попробуй фон попроще.\nТехнически: {0}', str(error)[-900:])
        )
        if env_bool("LOG_RENDER_REQUESTS", True):
            await log_to_owner_chat(
                context,
                f"{tg_emoji('error')} <b>Рендер упал</b>\n"
                f"{user_html(message.from_user) if message.from_user else f'<code>{user_id}</code>'}\n"
                f"{tg_emoji('gif')} {html.escape(source.label)} · {settings.width}×{settings.height} "
                f"{output_format_label(settings.output_format)}\n"
                f"{tg_emoji('warning')} <code>{html.escape(str(error)[-300:])}</code>",
            )
    finally:
        if (loading_shown and not sent_message and edit_target and edit_target.file_id
                and REFRESH_REVISIONS.get(user_id) == refresh_revision
                and LAST_RESULT.get(user_id) == edit_target):
            try:
                media_type = {"video": InputMediaVideo, "file": InputMediaDocument}.get(
                    edit_target.output_format, InputMediaAnimation,
                )
                await context.bot.edit_message_media(
                    chat_id=edit_target.chat_id, message_id=edit_target.message_id,
                    media=media_type(media=edit_target.file_id, caption=edit_target.caption, parse_mode=ParseMode.HTML if edit_target.caption_html else None),
                    read_timeout=60, write_timeout=120, connect_timeout=30, pool_timeout=60,
                )
            except TelegramError:
                logging.exception("Failed to restore the previous render after a failed refresh")
        BUSY.discard(user_id)
        await gate.release()
        shutil.rmtree(job_dir, ignore_errors=True)
    return sent_message


async def refresh_result(app: Application, user_id: int) -> None:
    try:
        while True:
            revision = REFRESH_REVISIONS[user_id]
            await asyncio.sleep(0.3)
            if revision != REFRESH_REVISIONS[user_id]:
                continue
            message, chat_id = REFRESH_REQUESTS[user_id]
            target = LAST_RESULT.get(user_id)
            if not target or target.chat_id != chat_id:
                return
            if user_id in BUSY or get_render_gate().active >= get_render_gate().limit:
                continue
            LANGUAGE.set(await asyncio.to_thread(selected_language, user_id) or LANGUAGE.get())
            await process_source(
                message, app, target.source, settings_for(user_id), actor_user_id=user_id,
                charge_rate=False, edit_target=target, refresh_revision=revision,
            )
            if user_id in BUSY or get_render_gate().active >= get_render_gate().limit:
                continue
            if revision == REFRESH_REVISIONS[user_id]:
                return
    finally:
        REFRESH_TASKS.pop(user_id, None)
        REFRESH_REQUESTS.pop(user_id, None)


async def settings_changed(app: Application, update: Update) -> None:
    user_id = update.effective_user.id
    await asyncio.to_thread(save_render_session, user_id)
    message = update.effective_message
    chat = update.effective_chat
    if not message or not chat:
        return
    if user_id not in LAST_RESULT:
        if user_id not in MISSING_RESULT_NOTICE:
            with db_connect() as conn:
                row = conn.execute("SELECT render_count FROM users WHERE user_id=?", (user_id,)).fetchone()
            if row and row["render_count"]:
                MISSING_RESULT_NOTICE.add(user_id)
                await message.reply_text(t("Пришли исходник ещё раз — дальше буду обновлять этот результат при смене настроек."))
        return
    REFRESH_REVISIONS[user_id] = REFRESH_REVISIONS.get(user_id, 0) + 1
    REFRESH_REQUESTS[user_id] = message, chat.id
    if user_id not in REFRESH_TASKS:
        REFRESH_TASKS[user_id] = app.create_task(refresh_result(app, user_id), update=update)


def mode_intro_text() -> str:
    return t('{0} <b>StickerLoop</b>\n\n{1} <b>Стикеры → GIF</b>\nПришли стикер, custom emoji или ссылку на стикерпак. Фон и размер можно поменять в настройках.', tg_emoji("bot"), tg_emoji("gif"))



def mode_menu_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [menu_button(t("Стикеры → GIF"), "mode:loop", "gif")],
        [menu_button(t("Настройки"), "mode:settings", "settings"),
         menu_button(t("Помощь"), "mode:help", "help")],
        [menu_button(t("Страна / язык"), "lang:menu:home", "globe")],
    ])



HELP_TEXT = '{0} <b>Как пользоваться</b>\n\n{1} Пришли стикер, custom emoji или ссылку на стикерпак — получишь зацикленный GIF.\n{2} В настройках можно выбрать фон, размер, перекраску, подпись и вотермарку.\n{3} После смены настроек результат обновится в том же сообщении. Пока он готовится, показывается анимация загрузки.\n{4} Страну и язык можно выбрать через меню.'

def help_text() -> str:
    return t(HELP_TEXT, tg_emoji("info"), tg_emoji("gif"), tg_emoji("settings"), tg_emoji("loading"), tg_emoji("globe"))


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message:
        return
    await remember_user(update, context, "help")
    await update.message.reply_text(
        help_text(), parse_mode=ParseMode.HTML, disable_web_page_preview=True,
        reply_markup=InlineKeyboardMarkup([[menu_button(t('Главное меню'), "mode:home", "back")]]),
    )


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not update.effective_user:
        return
    await remember_user(update, context, "start")
    if not await asyncio.to_thread(selected_language, update.effective_user.id):
        await update.message.reply_text(
            f"{tg_emoji('globe')} {html.escape(t('Выберите страну'))}",
            parse_mode=ParseMode.HTML,
            reply_markup=language_keyboard("home", allow_back=False),
        )
        return
    await update.message.reply_text(
        mode_intro_text(),
        parse_mode=ParseMode.HTML,
        reply_markup=mode_menu_keyboard(),
        disable_web_page_preview=True,
    )


def language_keyboard(destination: str, *, allow_back: bool = True) -> InlineKeyboardMarkup:
    items = list(COUNTRIES.items())
    rows = [
        [InlineKeyboardButton(
            name, callback_data=f"lang:set:{country}:{destination}",
            icon_custom_emoji_id=emoji_id,
        ) for country, (name, _, emoji_id) in items[index:index + 2]]
        for index in range(0, len(items), 2)
    ]
    if allow_back:
        callback = "menu:main" if destination == "settings" else "mode:home"
        rows.append([menu_button(t("Назад"), callback)])
    return InlineKeyboardMarkup(rows)


async def on_language_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not query or not query.message or not update.effective_user:
        return
    parts = (query.data or "").split(":")
    if len(parts) not in (3, 4) or parts[0] != "lang":
        await query.answer()
        return
    action = parts[1]
    destination = parts[-1]
    if destination not in {"home", "settings"}:
        await query.answer()
        return
    if action == "menu" and len(parts) == 3:
        await query.answer()
        PENDING_ACTIONS.pop(update.effective_user.id, None)
        await edit_menu_message(query.message, f"{tg_emoji('globe')} {html.escape(t('Выберите страну'))}", language_keyboard(destination))
        return
    if action != "set" or len(parts) != 4 or parts[2] not in COUNTRIES:
        await query.answer()
        return
    country = parts[2]
    _, code, _ = COUNTRIES[country]
    await remember_user(update, context, "language")

    def save() -> None:
        with db_connect() as conn:
            conn.execute("UPDATE users SET ui_language = ?, country_code = ? WHERE user_id = ?",
                         (code, country, update.effective_user.id))

    await asyncio.to_thread(save)
    LANGUAGE.set(code)
    PENDING_ACTIONS.pop(update.effective_user.id, None)
    await query.answer(t("Страна и язык сохранены"))
    if destination == "settings":
        current = settings_for(update.effective_user.id)
        await edit_menu_message(query.message, settings_summary(current), main_menu_keyboard(current))
    else:
        await edit_menu_message(query.message, mode_intro_text(), mode_menu_keyboard())


async def on_mode_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not query or not query.from_user or not query.message:
        return
    await query.answer()
    uid = query.from_user.id
    PENDING_ACTIONS.pop(uid, None)
    mode = (query.data or "").split(":", 1)[-1]
    if mode == "settings":
        await send_menu_message(query.message, settings_for(uid))
        return
    if mode == "help":
        await edit_menu_message(query.message, help_text(),
            InlineKeyboardMarkup([[menu_button(t("Главное меню"), "mode:home")]]))
        return
    await edit_menu_message(query.message, mode_intro_text(), mode_menu_keyboard())



async def settings(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not update.effective_user:
        return
    await remember_user(update, context, "settings")
    current = settings_for(update.effective_user.id)
    await send_menu_message(update.message, current)


async def limits(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message:
        return
    await remember_user(update, context, "limits")
    config = safety_config()
    await update.message.reply_text(
        t(
            '{6} <b>Лимиты</b>\n'
            '- одновременно рендерится максимум {0} (сейчас активно {1})\n'
            '- у одного пользователя максимум 1 активная задача\n'
            '- не больше {2} рендеров за {3}\n'
            '- спам-пауза: {4}\n'
            '- таймаут рендера: {5}',
            config.max_global_renders,
            get_render_gate().active,
            config.per_user_window_jobs,
            format_duration(config.per_user_window_seconds),
            format_duration(config.ban_seconds),
            format_duration(config.render_timeout_seconds),
            tg_emoji("settings"),
        ),
        parse_mode=ParseMode.HTML,
    )


async def users_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message:
        return
    await remember_user(update, context, "admin:users")
    if not await require_admin(update, context):
        return
    stats = await asyncio.to_thread(user_stats)
    await update.message.reply_text(
        f"{tg_emoji('stats')} <b>Статистика</b>\n\n"
        f"{tg_emoji('users')} Всего пользователей: {stats['total']}\n"
        f"{tg_emoji('message')} Доступны для рассылки: {stats['reachable']}\n"
        f"{tg_emoji('gif')} Всего рендеров: {stats['renders']}\n\n"
        f"{tg_emoji('calendar')} <b>За неделю</b>\n"
        f"{tg_emoji('new')} Новых: {stats['new_week']}\n"
        f"{tg_emoji('active')} Активных: {stats['active_week']}\n"
        f"{tg_emoji('gif')} Рендеров: {stats['renders_week']}",
        parse_mode=ParseMode.HTML,
    )


async def whoami(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not update.effective_user:
        return
    await remember_user(update, context, "whoami")
    await update.message.reply_text(t('Твой Telegram ID: {0}', update.effective_user.id))


async def start_menu_asset_mode(update: Update, context: ContextTypes.DEFAULT_TYPE, section: str) -> None:
    if not update.message or not update.effective_user:
        return
    section = normalize_menu_asset_section(section)
    await remember_user(update, context, f"admin:menu_assets:{section}")
    if not await require_admin(update, context):
        return
    label = menu_asset_section_label(section)
    reply = await update.message.reply_text(
        f"{tg_emoji('media')} <b>Режим добавления GIF: {html.escape(label)}.</b>\n"
        "Кидай sticker, premium/custom emoji, фото, видео или GIF.\n"
        "Бот отрендерит и сохранит как верхнюю карточку нужного раздела.\n\n"
        f"Сейчас в разделе: {menu_asset_count(section)}\n"
        "- чтобы закончить.",
        parse_mode=ParseMode.HTML,
        reply_markup=back_keyboard(),
    )
    PENDING_ACTIONS[update.effective_user.id] = pending_from_message(menu_asset_action(section), reply)


async def menu_assets_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    raw_section = context.args[0] if context.args else "main"
    await start_menu_asset_mode(update, context, raw_section)


async def menu_asset_palette_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await start_menu_asset_mode(update, context, "palette")


def build_broadcast_draft(update: Update, target_user_ids: Sequence[int]) -> BroadcastDraft | None:
    if not update.message or not update.effective_user:
        return None

    if update.message.reply_to_message:
        replied = update.message.reply_to_message
        return BroadcastDraft(
            sender_id=update.effective_user.id,
            created_at=time.time(),
            target_user_ids=tuple(target_user_ids),
            copy_from_chat_id=replied.chat_id,
            copy_message_id=replied.message_id,
        )

    text = update.message.text or ""
    parts = text.split(maxsplit=1)
    if len(parts) < 2 or not parts[1].strip():
        return None
    return BroadcastDraft(
        sender_id=update.effective_user.id,
        created_at=time.time(),
        target_user_ids=tuple(target_user_ids),
        text=parts[1].strip(),
    )


async def broadcast_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message:
        return
    await remember_user(update, context, "admin:broadcast")
    if not await require_admin(update, context):
        return

    target_user_ids = await asyncio.to_thread(known_user_ids)
    if not target_user_ids:
        await update.message.reply_text("Пока нет пользователей для рассылки.")
        return

    draft = build_broadcast_draft(update, target_user_ids)
    if not draft:
        await update.message.reply_text(
            "Использование:\n"
            "/broadcast текст сообщения\n"
            "или ответь /broadcast на сообщение, которое нужно скопировать всем."
        )
        return

    draft_id = secrets.token_urlsafe(4)
    BROADCAST_DRAFTS[draft_id] = draft
    kind = "копия сообщения" if draft.copy_message_id else "текст"
    preview = draft.text[:500] if draft.text else kind
    await update.message.reply_text(
        "Черновик рассылки создан.\n"
        f"id: {draft_id}\n"
        f"тип: {kind}\n"
        f"получателей: {len(target_user_ids)}\n"
        f"превью: {preview}\n\n"
        f"Отправить: /broadcast_send {draft_id}\n"
        f"Отменить: /broadcast_cancel {draft_id}"
    )


async def send_broadcast_message(
    context: ContextTypes.DEFAULT_TYPE,
    user_id: int,
    draft: BroadcastDraft,
) -> bool:
    try:
        if draft.copy_message_id and draft.copy_from_chat_id:
            await context.bot.copy_message(
                chat_id=user_id,
                from_chat_id=draft.copy_from_chat_id,
                message_id=draft.copy_message_id,
                read_timeout=20,
                connect_timeout=20,
            )
        elif draft.text:
            await context.bot.send_message(
                chat_id=user_id,
                text=draft.text,
                disable_web_page_preview=False,
                read_timeout=20,
                connect_timeout=20,
            )
        return True
    except Forbidden:
        await asyncio.to_thread(mark_user_blocked, user_id)
    except BadRequest as error:
        if "chat not found" in str(error).lower() or "bot was blocked" in str(error).lower():
            await asyncio.to_thread(mark_user_blocked, user_id)
        else:
            logging.warning("Broadcast bad request for %s: %s", user_id, error)
    except TelegramError:
        logging.exception("Broadcast failed for %s", user_id)
    return False


async def broadcast_send(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message:
        return
    await remember_user(update, context, "admin:broadcast_send")
    if not await require_admin(update, context):
        return

    if not context.args:
        await update.message.reply_text("Укажи id: /broadcast_send <id>")
        return

    draft_id = context.args[0]
    draft = BROADCAST_DRAFTS.get(draft_id)
    if not draft:
        await update.message.reply_text("Черновик не найден или уже отправлен.")
        return

    if time.time() - draft.created_at > env_int("BROADCAST_DRAFT_TTL_SECONDS", 1800):
        BROADCAST_DRAFTS.pop(draft_id, None)
        await update.message.reply_text("Черновик устарел. Создай новый /broadcast.")
        return

    await update.message.reply_text(f"Начинаю рассылку на {len(draft.target_user_ids)} пользователей.")
    sent = 0
    failed = 0
    delay = env_float("BROADCAST_DELAY_SECONDS", 0.05)
    for user_id in draft.target_user_ids:
        ok = await send_broadcast_message(context, user_id, draft)
        if ok:
            sent += 1
        else:
            failed += 1
        if delay > 0:
            await asyncio.sleep(delay)

    BROADCAST_DRAFTS.pop(draft_id, None)
    await update.message.reply_text(f"Рассылка завершена. Отправлено: {sent}, ошибок: {failed}.")
    await log_to_owner_chat(
        context,
        f"{tg_emoji('send')} <b>Рассылка завершена</b>\n"
        f"{user_html(update.effective_user)}\n"
        f"{tg_emoji('check')} доставлено: {sent} · {tg_emoji('error')} ошибок: {failed}",
    )


async def broadcast_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message:
        return
    await remember_user(update, context, "admin:broadcast_cancel")
    if not await require_admin(update, context):
        return

    if not context.args:
        await update.message.reply_text("Укажи id: /broadcast_cancel <id>")
        return
    draft_id = context.args[0]
    if BROADCAST_DRAFTS.pop(draft_id, None):
        await update.message.reply_text("Черновик рассылки отменен.")
    else:
        await update.message.reply_text("Черновик не найден.")


HEX_RE = re.compile(r"^#?(?:[0-9a-fA-F]{3}|[0-9a-fA-F]{6})$")
RESOLUTION_RE = re.compile(r"^(\d{2,4})\s*[xх×]\s*(\d{2,4})(?:\s+(\d{1,3})\s*fps)?$", re.IGNORECASE)
RATIO_RE = re.compile(r"^(\d+(?:\.\d+)?)\s*:\s*(\d+(?:\.\d+)?)$")


def normalize_hex(value: str) -> str:
    color = value.strip()
    if not HEX_RE.match(color):
        raise ValueError("Use #RGB or #RRGGBB")
    if not color.startswith("#"):
        color = "#" + color
    if len(color) == 4:
        color = "#" + "".join(ch * 2 for ch in color[1:])
    return color.lower()


def parse_resolution(value: str, current: RenderSettings) -> tuple[int, int, int]:
    text = value.strip().lower().replace(",", ".")
    fps = current.fps
    match = RESOLUTION_RE.match(text)
    if match:
        width = int(match.group(1))
        height = int(match.group(2))
        if match.group(3):
            fps = int(match.group(3))
    else:
        ratio = RATIO_RE.match(text)
        if not ratio:
            raise ValueError("bad resolution")
        left = float(ratio.group(1))
        right = float(ratio.group(2))
        if left <= 0 or right <= 0:
            raise ValueError("bad ratio")
        width = current.width
        height = round(width * right / left)

    max_width = env_int("MAX_OUTPUT_WIDTH", 1920)
    max_height = env_int("MAX_OUTPUT_HEIGHT", 1080)
    max_pixels = env_int("MAX_OUTPUT_PIXELS", 1920 * 1080)
    max_fps = env_int("MAX_OUTPUT_FPS", 60)
    if width < 64 or height < 64 or width > max_width or height > max_height:
        raise ValueError("resolution out of range")
    if width * height > max_pixels:
        raise ValueError("too many pixels")
    if fps < 12 or fps > max_fps:
        raise ValueError("fps out of range")
    return width, height, fps


def pack_name_from_link(text: str) -> str | None:
    match = re.search(r"(?:t\.me|telegram\.me)/(?:addstickers|addemoji)/([A-Za-z0-9_]+)", text)
    if match:
        return match.group(1)
    return None


async def bg(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not update.effective_user:
        return
    await remember_user(update, context, "bg")

    if context.args:
        try:
            color = normalize_hex(context.args[0])
        except ValueError:
            await update.message.reply_text(t('Цвет нужен в формате /bg #101820'))
            return
        update_settings(
            update.effective_user.id,
            background_key="custom",
            background_hex=color,
            gradient_end_hex=None,
            gradient_direction=None,
        )
        await update.message.reply_text(
            t('Поставил фон {0}. Кидай стикер.', color),
            reply_markup=main_menu_keyboard(settings_for(update.effective_user.id)),
        )
        return

    await update.message.reply_text(t('Выбери фон или отправь /bg #101820'), reply_markup=background_menu_keyboard(update.effective_user.id))


async def on_background_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not query or not query.from_user or not query.message:
        return
    await remember_user(update, context, "bg_callback")

    await query.answer()
    key = (query.data or "").removeprefix("bg:")
    if key not in BACKGROUND_PRESETS:
        return

    name, color = BACKGROUND_PRESETS[key]
    update_settings(
        query.from_user.id,
        background_key=key,
        background_hex=color,
        gradient_end_hex=None,
        gradient_direction=None,
    )

    await query.message.reply_text(t('Фон: {0} {1}', name, color))


async def on_menu_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not query or not query.from_user or not query.message:
        return
    await remember_user(update, context, "menu_callback")
    await query.answer()

    user_id = query.from_user.id
    data = query.data or ""
    current = settings_for(user_id)

    if data in {"menu:main", "menu:preview", "menu:format", "menu:media"} or data.startswith("fmt:"):
        PENDING_ACTIONS.pop(user_id, None)
        await edit_menu_message(query.message, settings_summary(current), main_menu_keyboard(current))
        return
    if data == "menu:bg":
        shown = await show_section_menu_message(
            context,
            query.message,
            t(
                '{0} <b>Фон подложки</b>\n'
                'Сейчас: <b>{1}</b>\n'
                '\n'
                '• жми готовый пресет ниже\n'
                '• или пришли свой HEX-цвет сообщением: <code>FFFFFF</code>, <code>#1e90ff</code>\n'
                '• или выбери градиент / загрузи своё фото',
                tg_emoji('brush'),
                _bg_value_label(current),
            ),
            background_menu_keyboard(user_id),
            "palette",
        )
        if shown:
            PENDING_ACTIONS[user_id] = pending_from_message("bg", shown)
        return
    if data == "menu:bg_upload":
        PENDING_ACTIONS[user_id] = pending_from_message("bg_upload", query.message)
        await edit_menu_message(
            query.message,
            t(
                '{0} <b>Свой фон из фото</b>\n'
                '\n'
                'Пришли фото сообщением — оно станет подложкой.\n'
                'Картинка растянется под размер {1}×{2}.',
                tg_emoji('media'),
                current.width,
                current.height,
            ),
            back_keyboard(),
        )
        return
    if data.startswith("setbg:"):
        key = data.removeprefix("setbg:")
        if key in BACKGROUND_PRESETS:
            name, color = BACKGROUND_PRESETS[key]
            current = update_settings(user_id, background_key=key, background_hex=color, gradient_end_hex=None, gradient_direction=None)
            await edit_menu_message(query.message, settings_summary(current), main_menu_keyboard(current))
        return
    if data == "menu:gradient":
        await safe_delete_message(query.message)
        await _send_gradient_preview(context, query.message.chat_id, current)
        return
    if data.startswith("setgradient:"):
        parts = data.removeprefix("setgradient:").split("/", 2)
        if len(parts) == 3:
            direction, c0, c1 = parts
            current = update_settings(user_id, background_key="gradient", background_hex=c0, gradient_end_hex=c1, gradient_direction=direction)
            await _send_gradient_preview(context, query.message.chat_id, current, message_id=query.message.message_id)
        return
    if data == "menu:resolution":
        await show_section_menu_message(
            context,
            query.message,
            t(
                '{0} <b>Размер и частота кадров</b>\n'
                'Сейчас: <b>{1}×{2} · {3} FPS</b>\n'
                '\n'
                'Выбери пресет или задай свой через «Свой размер…»',
                tg_emoji('resolution'),
                current.width,
                current.height,
                current.fps,
            ),
            resolution_menu_keyboard(current),
            "resolution",
        )
        return
    if data == "menu:res_custom":
        PENDING_ACTIONS[user_id] = pending_from_message("resolution", query.message)
        await edit_menu_message(
            query.message,
            t(
                '{0} <b>Свой размер</b>\n'
                'Сейчас: <b>{1}×{2} · {3} FPS</b>\n'
                '\n'
                'Пришли сообщением в одном из форматов:\n'
                '• <code>1920x600</code> — точный размер\n'
                '• <code>1280x720 60fps</code> — размер + частота кадров\n'
                '• <code>16:9</code> или <code>2.35:1</code> — пропорции (ширина останется текущей)',
                tg_emoji('resolution'),
                current.width,
                current.height,
                current.fps,
            ),
            back_keyboard(),
        )
        return
    if data.startswith("setres:"):
        key = data.removeprefix("setres:")
        if key in RESOLUTION_PRESETS:
            w, h, fps = RESOLUTION_PRESETS[key]
            current = update_settings(user_id, width=w, height=h, fps=fps)
            await edit_menu_message(query.message, settings_summary(current), main_menu_keyboard(current))
        return
    if data == "menu:item_color":
        await edit_menu_message(
            query.message,
            t(
                '{0} <b>Перекраска стикера/эмодзи</b>\n'
                'Сейчас: <b>{1}</b>\n'
                '\n'
                'Зальёт сам стикер одним цветом (силуэт), фон не трогает.\n'
                'Удобно под фирменный стиль канала или сайта.',
                tg_emoji('brush'),
                html.escape(current.item_color_hex if current.item_color_hex else t('выкл — исходные цвета')),
            ),
            item_color_keyboard(current.item_color_hex),
        )
        return
    if data.startswith("setcolor:"):
        key = data.removeprefix("setcolor:")
        if key in ITEM_COLOR_PRESETS:
            _, hex_color = ITEM_COLOR_PRESETS[key]
            current = update_settings(user_id, item_color_hex=hex_color)
            await edit_menu_message(
                query.message,
                t('{0} <b>Цвет перекраски emoji/sticker:</b>\nСейчас: {1}', tg_emoji('brush'), html.escape(current.item_color_hex)),
                item_color_keyboard(current.item_color_hex),
            )
        return
    if data == "menu:item_color_custom":
        shown = await show_section_menu_message(
            context,
            query.message,
            t(
                '{0} <b>Свой цвет перекраски</b>\n'
                '\n'
                'Пришли HEX-цвет сообщением: <code>FFFFFF</code>, <code>#e91e90</code>\n'
                'Отправь <code>-</code> чтобы вернуть исходные цвета.',
                tg_emoji('brush'),
            ),
            InlineKeyboardMarkup([[menu_button(t('Назад'), "menu:item_color")]]),
            "palette",
        )
        if shown:
            PENDING_ACTIONS[user_id] = pending_from_message("item_color", shown)
        return
    if data == "itemcolor:clear":
        current = update_settings(user_id, item_color_hex=None)
        await edit_menu_message(
            query.message,
            t('{0} <b>Цвет перекраски emoji/sticker:</b>\nСейчас: без перекраски', tg_emoji('brush')),
            item_color_keyboard(current.item_color_hex),
        )
        return
    if data == "menu:notes":
        sent = await show_section_menu_message(
            context,
            query.message,
            t(
                '{0} <b>Подпись под результатом</b>\n'
                'Сейчас: <b>{1}</b>\n'
                '\n'
                'Пришли текст сообщением — добавлю его под каждый готовый рендер.\n'
                'Отправь <code>-</code> чтобы убрать подпись.',
                tg_emoji('write'),
                html.escape(current.notes[:60]) if current.notes else t('нет'),
            ),
            notes_keyboard(),
            "notes",
        )
        if sent:
            PENDING_ACTIONS[user_id] = pending_from_message("notes", sent)
        else:
            PENDING_ACTIONS[user_id] = pending_from_message("notes", query.message)
        return
    if data == "notes:clear":
        current = update_settings(user_id, notes="")
        await edit_menu_message(query.message, settings_summary(current), main_menu_keyboard(current))
        return
    if data == "menu:watermark":
        wm_state = (
            t('вкл · «{0}»', html.escape(current.watermark_text))
            if current.watermark_enabled and current.watermark_text
            else t('выкл')
        )
        await show_section_menu_message(
            context,
            query.message,
            t('{0} <b>Вотермарка</b>\nСейчас: <b>{1}</b>\n\nПолупрозрачный текст в углу результата — например, имя канала.', tg_emoji('text'), wm_state),
            watermark_keyboard(current),
            "watermark",
        )
        return
    if data == "wm:toggle":
        current = update_settings(user_id, watermark_enabled=not current.watermark_enabled)
        await edit_menu_message(query.message, settings_summary(current), main_menu_keyboard(current))
        return
    if data == "wm:text":
        PENDING_ACTIONS[user_id] = pending_from_message("watermark_text", query.message)
        await edit_menu_message(
            query.message,
            t(
                '{0} <b>Текст вотермарки</b>\n'
                '\n'
                'Пришли текст сообщением (до 48 символов) — он появится в углу.\n'
                'Отправь <code>-</code> чтобы выключить вотермарку.',
                tg_emoji('text'),
            ),
            back_keyboard(),
        )
        return
    if data == "menu:reset":
        USER_SETTINGS.pop(user_id, None)
        USER_BG_IMAGES.pop(user_id, None)
        PENDING_ACTIONS.pop(user_id, None)
        current = default_settings()
        await edit_menu_message(
            query.message,
            t('{0} <b>Настройки сброшены к значениям по умолчанию.</b>\n\n{1}', tg_emoji('check'), settings_summary(current)),
            main_menu_keyboard(current),
        )
        return
    if data.startswith("setbgimg:reset"):
        key = data.removeprefix("setbgimg:reset:")
        USER_BG_IMAGES.pop(user_id, None)
        if key not in BACKGROUND_PRESETS:
            key = "dark"
        _, hex_color = BACKGROUND_PRESETS[key]
        current = update_settings(user_id, background_key=key, background_hex=hex_color)
        await edit_menu_message(
            query.message,
            t('{0} <b>Фон сброшен на цвет.</b>\n\n{1}', tg_emoji('check'), settings_summary(current)),
            main_menu_keyboard(current),
        )
        return
    if data == "menu:support":
        await edit_menu_message(
            query.message,
            t(
                '{0} <b>Поддержать разработчика</b>\n'
                '\n'
                'Бот бесплатный и с открытым кодом.\n'
                'Если хочешь поддержать — звёздочка на GitHub решает:\n'
                'github.com/LimeWombat/telegram-sticker-loop-bot\n'
                '\n'
                'Или напиши @lewombats — ideas, баги, спасибо {1}',
                tg_emoji('stars'),
                tg_emoji('heart'),
            ),
            main_menu_keyboard(current),
        )
        return


async def handle_pending_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    if not update.message or not update.effective_user or not update.message.text:
        return False
    user_id = update.effective_user.id
    pending = PENDING_ACTIONS.get(user_id)
    if not pending:
        return False

    action = pending.action
    text = update.message.text.strip()
    current = settings_for(user_id)
    try:
        if action == "bg":
            color = normalize_hex(text)
            current = update_settings(user_id, background_key="custom", background_hex=color,
                                      gradient_end_hex=None, gradient_direction=None)
            await safe_delete_message(update.message)
            await edit_pending_menu(context, pending, settings_summary(current), main_menu_keyboard(current))
        elif action == "resolution":
            width, height, fps = parse_resolution(text, current)
            current = update_settings(user_id, width=width, height=height, fps=fps)
            await safe_delete_message(update.message)
            await edit_pending_menu(context, pending, settings_summary(current), main_menu_keyboard(current))
        elif action == "item_color":
            if text in {"-", "0", "off", "нет"}:
                current = update_settings(user_id, item_color_hex=None)
            else:
                color = normalize_hex(text)
                current = update_settings(user_id, item_color_hex=color)
            await safe_delete_message(update.message)
            await edit_pending_menu(context, pending, settings_summary(current), main_menu_keyboard(current))
        elif action == "notes":
            notes = "" if text == "-" else text[:800]
            current = update_settings(user_id, notes=notes)
            await safe_delete_message(update.message)
            await edit_pending_menu(context, pending, settings_summary(current), main_menu_keyboard(current))
        elif action == "watermark_text":
            if text == "-":
                current = update_settings(user_id, watermark_enabled=False, watermark_text="")
            else:
                current = update_settings(user_id, watermark_enabled=True, watermark_text=text[:48])
            await safe_delete_message(update.message)
            await edit_pending_menu(context, pending, settings_summary(current), main_menu_keyboard(current))
        elif action.startswith("menu_asset"):
            section = menu_asset_section_from_action(action)
            if text == "-":
                PENDING_ACTIONS.pop(user_id, None)
                await safe_delete_message(update.message)
                await edit_pending_menu(
                    context,
                    pending,
                    t(
                        '{0} <b>Режим добавления GIF в меню выключен.</b>\n'
                        'Раздел: {1}\n'
                        'Сейчас в разделе: {2}',
                        tg_emoji('check'),
                        html.escape(menu_asset_section_label(section)),
                        menu_asset_count(section),
                    ),
                    main_menu_keyboard(current),
                )
            else:
                await safe_delete_message(update.message)
                await edit_pending_menu(
                    context,
                    pending,
                    t(
                        '{0} <b>Кидай sticker/emoji/media для раздела {1}.</b>\n'
                        '- чтобы закончить.',
                        tg_emoji('media'),
                        html.escape(menu_asset_section_label(section)),
                    ),
                    back_keyboard(),
                )
            return True
    except ValueError:
        await safe_delete_message(update.message)
        await edit_pending_menu(
            context,
            pending,
            t('{0} <b>Не понял формат.</b>\nПопробуй еще раз или нажми Назад.', tg_emoji('info')),
            back_keyboard(),
        )
        return True

    PENDING_ACTIONS.pop(user_id, None)
    return True


def media_source_from_message(message: Message) -> SourceRef | None:
    if message.photo:
        return SourceRef(message.photo[-1].file_id, "custom photo")
    if message.video:
        return SourceRef(message.video.file_id, "custom video")
    if message.animation:
        return SourceRef(message.animation.file_id, "custom animation")
    if message.document:
        return SourceRef(message.document.file_id, "custom document")
    return None


async def on_media(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not update.effective_user:
        return

    pending = PENDING_ACTIONS.get(update.effective_user.id)
    if pending and pending.action == "bg_upload":
        photo = update.message.photo
        if not photo or not photo[-1]:
            await update.message.reply_text(t('Отправь именно фото (не файл, не стикер).'))
            return
        file_id = photo[-1].file_id
        bg_file = await context.bot.get_file(file_id)
        bg_path = BG_DIR / f"{update.effective_user.id}.jpg"
        BG_DIR.mkdir(parents=True, exist_ok=True)
        await bg_file.download_to_drive(bg_path)
        USER_BG_IMAGES[update.effective_user.id] = bg_path
        PENDING_ACTIONS.pop(update.effective_user.id, None)
        current = settings_for(update.effective_user.id)
        await send_menu_message(update.message, current)
        await update.message.reply_text(f"{tg_emoji('media')} {html.escape(t('Фон загружен! Кидай стикер.'))}", parse_mode=ParseMode.HTML)
        return

    source = media_source_from_message(update.message)
    if not source:
        return
    if pending and pending.action.startswith("menu_asset") and await is_admin_user(update, context):
        await process_menu_asset(
            update.message,
            context,
            source,
            update.effective_user.id,
            menu_asset_section_from_action(pending.action),
        )
        return
    await update.message.reply_text(t('Пришли стикер, custom emoji или ссылку на стикерпак.'))


async def process_menu_asset(
    message: Message,
    context: ContextTypes.DEFAULT_TYPE,
    source: SourceRef,
    user_id: int,
    section: str = "main",
) -> None:
    section = normalize_menu_asset_section(section)
    settings = replace(
        settings_for(user_id),
        output_format="gif",
        notes="",
        watermark_enabled=False,
        width=env_int("MENU_ASSET_WIDTH", 640),
        height=env_int("MENU_ASSET_HEIGHT", 360),
    )
    sent = await process_source(message, context, source, settings, actor_user_id=user_id, charge_rate=False)
    if sent and sent.animation:
        await asyncio.to_thread(add_menu_asset, sent.animation.file_id, section)
        await message.reply_text(
            f"Добавил GIF в раздел: {menu_asset_section_label(section)}. Всего: {menu_asset_count(section)}\n"
            "Кидай следующий или отправь '-' чтобы закончить.",
        )


async def on_sticker(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not update.message.sticker or not update.effective_user:
        return
    source = source_from_sticker(update.message.sticker)
    pending = PENDING_ACTIONS.get(update.effective_user.id)
    if pending and pending.action.startswith("menu_asset") and await is_admin_user(update, context):
        await process_menu_asset(
            update.message,
            context,
            source,
            update.effective_user.id,
            menu_asset_section_from_action(pending.action),
        )
        return
    PENDING_ACTIONS.pop(update.effective_user.id, None)
    await remember_user(
        update,
        context,
        "sticker",
        render_started=True,
        source_label=source.label,
        source_message=update.message,
    )
    LAST_SOURCE[update.effective_user.id] = source
    await process_source(update.message, context, source, settings_for(update.effective_user.id))


async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not update.effective_user:
        return

    ids = custom_emoji_ids(update.message)
    pending = PENDING_ACTIONS.get(update.effective_user.id)
    if pending and pending.action.startswith("menu_asset") and ids and await is_admin_user(update, context):
        section = menu_asset_section_from_action(pending.action)
        limit = max(1, min(env_int("MAX_CUSTOM_EMOJI_RENDER_ITEMS", 5), 10))
        stickers: Iterable[Sticker] = await context.bot.get_custom_emoji_stickers(ids[:limit], read_timeout=30)
        for sticker in stickers:
            await process_menu_asset(
                update.message,
                context,
                source_from_sticker(sticker),
                update.effective_user.id,
                section,
            )
        return

    if await handle_pending_text(update, context):
        return

    pack_name = pack_name_from_link(update.message.text or "")

    if not ids:
        if pack_name:
            await remember_user(update, context, "pack_link", render_started=True)
            try:
                sticker_set = await context.bot.get_sticker_set(pack_name, read_timeout=30)
            except TelegramError:
                logging.exception("Failed to load sticker set %s", pack_name)
                await update.message.reply_text(t('Не смог открыть этот pack. Проверь ссылку.'))
                return
            limit = max(1, min(env_int("MAX_PACK_RENDER_ITEMS", 3), 10))
            stickers = list(sticker_set.stickers[:limit])
            if not stickers:
                await update.message.reply_text(t('В этом pack не нашел стикеров.'))
                return
            await update.message.reply_text(t('Нашел pack, рендерю первые {0} шт.', len(stickers)))
            for index, sticker in enumerate(stickers):
                source = source_from_sticker(sticker)
                LAST_SOURCE[update.effective_user.id] = source
                await process_source(
                    update.message,
                    context,
                    source,
                    settings_for(update.effective_user.id),
                    charge_rate=index == 0,
                )
            return

        await remember_user(update, context, "text")
        await update.message.reply_text(t('Пришли стикер, custom emoji или ссылку на стикерпак.'))
        return

    limit = max(1, min(env_int("MAX_CUSTOM_EMOJI_RENDER_ITEMS", 5), 10))
    stickers: Iterable[Sticker] = await context.bot.get_custom_emoji_stickers(ids[:limit], read_timeout=30)
    sticker_list = list(stickers)
    if not sticker_list:
        await update.message.reply_text(t('Не смог получить файл этого custom emoji.'))
        return

    source = source_from_sticker(sticker_list[0])
    await remember_user(
        update,
        context,
        "custom_emoji",
        render_started=True,
        source_label=source.label,
        source_message=update.message,
    )
    for index, sticker in enumerate(sticker_list):
        source = source_from_sticker(sticker)
        LAST_SOURCE[update.effective_user.id] = source
        await process_source(
            update.message,
            context,
            source,
            settings_for(update.effective_user.id),
            charge_rate=index == 0,
        )


async def on_inline_query(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.inline_query
    if not query or not query.from_user:
        return

    user_id = query.from_user.id
    settings = settings_for(user_id)
    source = LAST_SOURCE.get(user_id)

    if source:
        results = await _render_inline_result(context, user_id, source, settings, "last")
        try:
            await query.answer(results, cache_time=30, is_personal=True)
        except TelegramError:
            pass
        return

    results = [
        InlineQueryResultArticle(
            id="help",
            title="Sticker Loop Bot",
            description=t('Сначала отправь стикер в бота, затем возвращайся сюда'),
            input_message_content=InputTextMessageContent(
                t('Отправь стикер или emoji в @{0} чтобы получить анимацию', context.bot.username or 'StickerLoopBot')
            ),
        )
    ]
    try:
        await query.answer(results, cache_time=0, is_personal=True)
    except TelegramError:
        pass


async def _render_inline_result(
    context: ContextTypes.DEFAULT_TYPE,
    user_id: int,
    source: SourceRef,
    settings: RenderSettings,
    cache_key: str,
) -> list:
    inline_settings = replace(settings, width=320, height=320, fps=15, output_format="gif")

    try:
        job_dir = Path(tempfile.mkdtemp(prefix="inline-", dir=RUNS_DIR))
        try:
            source_path = await download_source(context, source, job_dir)
            output = await asyncio.to_thread(render_source, source_path, job_dir, inline_settings, user_id)

            with output.open("rb") as f:
                sent = await context.bot.send_video(
                    chat_id=user_id,
                    video=f,
                    supports_streaming=True,
                    disable_notification=True,
                    read_timeout=60,
                    write_timeout=60,
                )
            if not sent.video or not sent.video.file_id:
                raise RuntimeError("Video upload returned no file_id")
            file_id = sent.video.file_id
            await sent.delete()

            return [
                InlineQueryResultCachedVideo(
                    id=cache_key[:64],
                    video_file_id=file_id,
                    title=f"{settings.width}x{settings.height} | {source.label}",
                    description=f"{settings.background_hex} | {output_format_label(settings.output_format)}",
                )
            ]
        finally:
            shutil.rmtree(job_dir, ignore_errors=True)
    except Exception:
        logging.exception("Inline render failed for %s", source.label)

    return [
        InlineQueryResultArticle(
            id="error",
            title=t('Не вышло'),
            description=t('Попробуй ещё раз в боте'),
            input_message_content=InputTextMessageContent(t('Не смог собрать. Попробуй в @StickerLoopBot')),
        )
    ]


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    if isinstance(context.error, Conflict):
        logging.critical(
            "Polling conflict detected. Another getUpdates request used this bot token; exiting for systemd restart."
        )
        os._exit(75)
    if update is None and isinstance(context.error, NetworkError):
        # сетевой сбой самого polling (Bad Gateway/таймаут Telegram) — PTB сам ретраит
        logging.warning("Polling network error: %s", context.error)
        return
    logging.exception("Unhandled error for update %s", update, exc_info=context.error)


async def safe_startup_api_call(label: str, awaitable) -> None:
    try:
        await awaitable
    except RetryAfter as error:
        logging.warning(
            "Skipping startup Telegram API call %s after flood control: retry_after=%s",
            label,
            getattr(error, "retry_after", "unknown"),
        )
    except TelegramError:
        logging.exception("Startup Telegram API call failed: %s", label)


async def post_init(app: Application) -> None:
    public_commands = [
        ("start", "что умеет бот"),
        ("settings", "настройки рендера"),
        ("help", "как пользоваться"),
    ]
    admin_commands = [
        *public_commands,
        ("users", "админ: статистика пользователей"),
        ("menu_assets", "админ: GIF для меню"),
        ("menu_asset_palette", "админ: GIF для палитры"),
        ("broadcast", "админ: черновик рассылки"),
        ("broadcast_send", "админ: отправить рассылку"),
        ("broadcast_cancel", "админ: отменить рассылку"),
    ]

    if env_bool("SYNC_BOT_PROFILE_ON_STARTUP", False):
        await safe_startup_api_call("set_my_name", app.bot.set_my_name("Sticker Loop GIF"))
        await safe_startup_api_call(
            "set_my_short_description",
            app.bot.set_my_short_description(
                "Делаю GIF из Telegram стикеров, premium emoji и custom emoji. Фон на выбор."
            ),
        )
        await safe_startup_api_call(
            "set_my_description",
            app.bot.set_my_description(
                "Отправь animated sticker, video sticker, premium emoji или custom emoji. "
                "Бот соберет зацикленную MP4-анимацию как GIF: темный фон по умолчанию, "
                "цвета через /bg #101820. Поддержка .tgs и .webm стикеров, фоновые пресеты "
                "и аккуратные лимиты очереди."
            ),
        )

    await safe_startup_api_call(
        "set_my_commands:default",
        app.bot.set_my_commands(
            [(command, t(description, language="en")) for command, description in public_commands],
            scope=BotCommandScopeDefault(),
        ),
    )

    for code in LANGUAGES:
        await safe_startup_api_call(
            f"set_my_commands:{code}",
            app.bot.set_my_commands(
                [(command, t(description, language=code)) for command, description in public_commands],
                scope=BotCommandScopeDefault(), language_code="pt" if code == "pt-br" else code,
            ),
        )

    target = log_chat_id()
    if target:
        await safe_startup_api_call(
            "set_my_commands:log_chat_admins",
            app.bot.set_my_commands(admin_commands, scope=BotCommandScopeChatAdministrators(chat_id=target)),
        )

    for admin_id in parse_int_list(os.getenv("ADMIN_USER_IDS")):
        await safe_startup_api_call(
            f"set_my_commands:admin:{admin_id}",
            app.bot.set_my_commands(admin_commands, scope=BotCommandScopeChat(chat_id=admin_id)),
        )

    await _auto_generate_menu_assets(app)


class LocalizedApplication(Application):
    async def process_update(self, update: object) -> None:
        user = update.effective_user if isinstance(update, Update) else None
        chosen = await asyncio.to_thread(selected_language, user.id) if user else None
        language = chosen or normalize_language(user.language_code if user else "ru")
        token = LANGUAGE.set(language)
        before = settings_signature(user.id) if user else None
        try:
            await super().process_update(update)
            if user and before != settings_signature(user.id):
                await settings_changed(self, update)
        finally:
            LANGUAGE.reset(token)


def build_app(token: str) -> Application:
    return (
        ApplicationBuilder()
        .application_class(LocalizedApplication)
        .token(token)
        .post_init(post_init)
        .concurrent_updates(True)
        .build()
    )


def main() -> None:
    global GLOBAL_RENDER_GATE

    load_dotenv(ENV_PATH)
    setup_logging()
    RUNS_DIR.mkdir(parents=True, exist_ok=True)
    init_db()
    load_render_sessions()
    config = safety_config()
    GLOBAL_RENDER_GATE = RenderGate(config.max_global_renders)
    load_limit_state()
    load_menu_assets()
    cleanup_old_runs(config)

    token = os.getenv("BOT_TOKEN")
    if not token:
        raise SystemExit("BOT_TOKEN is missing. Put it in .env or export it.")
    if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
        raise SystemExit("ffmpeg and ffprobe are required.")

    app = build_app(token)
    app.add_handler(InlineQueryHandler(on_inline_query))
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CallbackQueryHandler(on_language_callback, pattern=r"^lang:"))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler(["settings", "menu"], settings))
    app.add_handler(CommandHandler("limits", limits))
    app.add_handler(CommandHandler("whoami", whoami))
    app.add_handler(CommandHandler("users", users_command))
    app.add_handler(CommandHandler("menu_assets", menu_assets_command))
    app.add_handler(CommandHandler("menu_asset_palette", menu_asset_palette_command))
    app.add_handler(CommandHandler("broadcast", broadcast_command))
    app.add_handler(CommandHandler("broadcast_send", broadcast_send))
    app.add_handler(CommandHandler("broadcast_cancel", broadcast_cancel))
    app.add_handler(CommandHandler("bg", bg))
    app.add_handler(
        CallbackQueryHandler(on_menu_callback, pattern=r"^(menu:|fmt:|setbg:|setres:|setcolor:|setgradient:|itemcolor:|notes:|wm:|setbgimg:|res:)")
    )
    app.add_handler(CallbackQueryHandler(on_background_callback, pattern=r"^bg:"))
    app.add_handler(CallbackQueryHandler(on_mode_callback, pattern=r"^(mode:|egrid:|etxt:)"))
    app.add_handler(MessageHandler(filters.Sticker.ALL, on_sticker))
    app.add_handler(MessageHandler(filters.PHOTO | filters.VIDEO | filters.Document.ALL | filters.ANIMATION, on_media))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    app.add_error_handler(error_handler)

    webhook_url = os.getenv("WEBHOOK_URL", "").strip()
    if webhook_url:
        webhook_path = os.getenv("WEBHOOK_PATH", "/webhook")
        webhook_port = env_int("WEBHOOK_PORT", 8000)
        webhook_secret = os.getenv("WEBHOOK_SECRET", secrets.token_hex(16))
        logging.info("Running with webhook on %s%s", webhook_url, webhook_path)
        app.run_webhook(
            listen="0.0.0.0",
            port=webhook_port,
            url_path=webhook_path,
            secret_token=webhook_secret,
            webhook_url=f"{webhook_url}{webhook_path}",
            allowed_updates=Update.ALL_TYPES,
            drop_pending_updates=True,
        )
    else:
        logging.info("Sticker loop bot is running with polling")
        app.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)


if __name__ == "__main__":
    main()
