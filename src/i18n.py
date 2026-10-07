"""Offline interface translations. User-provided values are inserted unchanged."""
from contextvars import ContextVar
from functools import lru_cache
import json
from pathlib import Path


LANGUAGES = {
    "ru": "Русский",
    "en": "English",
    "fa": "فارسی",
    "ar": "العربية",
    "tr": "Türkçe",
    "es": "Español",
    "pt-br": "Português (Brasil)",
    "id": "Bahasa Indonesia",
}
LANGUAGE: ContextVar[str] = ContextVar("interface_language", default="ru")


def normalize_language(value: str | None) -> str:
    code = (value or "en").lower().replace("_", "-")
    if code.startswith("pt"):
        return "pt-br"
    base = code.split("-", 1)[0]
    return base if base in LANGUAGES else "en"


@lru_cache(maxsize=len(LANGUAGES))
def catalog(language: str) -> dict[str, str]:
    path = Path(__file__).with_name("locales") / f"{language}.json"
    return json.loads(path.read_text(encoding="utf-8"))


def t(source: str, *values: object, language: str | None = None) -> str:
    code = normalize_language(language or LANGUAGE.get())
    template = source if code == "ru" else catalog(code).get(source, source)
    return template.format(*values) if values else template
