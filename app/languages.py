"""Languages a video can be voiced in.  Visuals carry no text, so any language here can be
added to an existing video later: only lyrics (Gemini), voice (TTS or sung) and metadata are new.

To add another language, add a row: code -> (English name, native name, Google TTS locale).
Voices: en/hi use the configured TTS_VOICE_EN / TTS_VOICE_HI; others use Google's default
female voice for the locale (override with env TTS_VOICE_<CODE>, e.g. TTS_VOICE_DE=de-DE-Neural2-C).
"""
from __future__ import annotations

import os

from . import config

LANGUAGES: dict[str, tuple[str, str, str]] = {
    "en": ("English", "English", "en-IN"),
    "hi": ("Hindi", "हिंदी", "hi-IN"),
    "ur": ("Urdu", "اردو", "ur-IN"),
    "bn": ("Bengali", "বাংলা", "bn-IN"),
    "mr": ("Marathi", "मराठी", "mr-IN"),
    "gu": ("Gujarati", "ગુજરાતી", "gu-IN"),
    "pa": ("Punjabi", "ਪੰਜਾਬੀ", "pa-IN"),
    "ta": ("Tamil", "தமிழ்", "ta-IN"),
    "te": ("Telugu", "తెలుగు", "te-IN"),
    "kn": ("Kannada", "ಕನ್ನಡ", "kn-IN"),
    "ml": ("Malayalam", "മലയാളം", "ml-IN"),
    "de": ("German", "Deutsch", "de-DE"),
    "fr": ("French", "Français", "fr-FR"),
    "es": ("Spanish", "Español", "es-ES"),
    "pt": ("Portuguese", "Português", "pt-BR"),
    "ar": ("Arabic", "العربية", "ar-XA"),
    "id": ("Indonesian", "Bahasa Indonesia", "id-ID"),
    "ja": ("Japanese", "日本語", "ja-JP"),
}


def name(code: str) -> str:
    return LANGUAGES[code][0]


def tts_locale(code: str) -> str:
    return LANGUAGES[code][2]


def tts_voice(code: str) -> str | None:
    """Explicit voice name, or None to let Google pick the default female voice for the locale."""
    return os.getenv(f"TTS_VOICE_{code.upper()}") or config.TTS_VOICES.get(code)


def listing() -> list[dict]:
    return [{"code": c, "name": n, "native": nat} for c, (n, nat, _) in LANGUAGES.items()]
