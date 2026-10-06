"""Systemsprache und Übersetzungen, auch für isolierte Helfer ohne Qt."""

from __future__ import annotations

import json
import os
import re
from functools import lru_cache
from pathlib import Path
from typing import Iterable

SUPPORTED_LANGUAGES = ("de", "en", "es")
_language: str | None = None


def choose_language(preferences: Iterable[str]) -> str:
    """Use the first supported UI language; unsupported systems use English."""
    for preference in preferences:
        code = re.split(r"[-_.@]", preference.strip(), maxsplit=1)[0].lower()
        if code in SUPPORTED_LANGUAGES:
            return code
    return "en"


def configure_language(preferences: Iterable[str]) -> str:
    global _language
    _language = choose_language(preferences)
    return _language


def environment_languages() -> list[str]:
    locale = (os.environ.get("LC_ALL") or os.environ.get("LC_MESSAGES")
              or os.environ.get("LANG") or "C")
    if locale in {"C", "POSIX"} or locale.startswith("C."):
        return [locale]
    return [*os.environ.get("LANGUAGE", "").split(":"), locale]


def language_preference(settings) -> str:
    """Accept only supported languages or the automatic desktop setting."""
    value = settings.value("language", "auto")
    return value if isinstance(value, str) and value in ("auto", *SUPPORTED_LANGUAGES) else "auto"


def initialize_language(preference: str | None = None) -> str:
    """Read the saved choice at startup; automatic mode follows the desktop."""
    try:
        from PyQt6.QtCore import QLocale, QSettings
        if preference is None:
            preference = language_preference(QSettings("PimpMyKernel", "PimpMyKernel"))
        if preference in SUPPORTED_LANGUAGES:
            return configure_language([preference])
        preferences = QLocale.system().uiLanguages()
    except ModuleNotFoundError:
        preferences = environment_languages()
    return configure_language([preference] if preference in SUPPORTED_LANGUAGES else preferences)


def language() -> str:
    global _language
    if _language is None:
        _language = choose_language(environment_languages())
    return _language


@lru_cache(maxsize=None)
def _catalog(code: str) -> dict[str, str]:
    if code == "de":
        return {}
    path = Path(__file__).with_name("assets") / "i18n" / f"{code}.json"
    return json.loads(path.read_text(encoding="utf-8"))


def tr(message: str, **values: object) -> str:
    code = language()
    translated = message if code == "de" else _catalog(code).get(
        message, _catalog("en").get(message, message))
    return translated.format(**values) if values else translated


def plural(singular: str, multiple: str, count: int, **values: object) -> str:
    return tr(singular if count == 1 else multiple, count=count, **values)


def install_qt_translations(app) -> bool:
    """Translate Qt's own buttons, context menus and file dialogs as well."""
    from PyQt6.QtCore import QLibraryInfo, QTranslator
    translator = QTranslator(app)
    directory = QLibraryInfo.path(QLibraryInfo.LibraryPath.TranslationsPath)
    if not translator.load(f"qtbase_{language()}", directory):
        return False
    app.installTranslator(translator)
    # QApplication must retain the Python wrapper for the entire session.
    app._pmk_qt_translator = translator
    return True
