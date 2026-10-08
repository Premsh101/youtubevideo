"""Central configuration: env vars, Vertex credentials, model names and price table.

Everything that costs money is listed in PRICES (USD) and converted to INR with
USD_TO_INR so the UI can show a rupee figure per video.  Prices are editable via
env (PRICE_<KEY>=0.01) because Google changes them; defaults are list prices at
the time of writing.
"""
from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = Path(os.getenv("DATA_DIR", ROOT / "data"))
CHARACTER_DIR = DATA_DIR / "characters"
CACHE_DIR = DATA_DIR / "cache"
OUTPUT_DIR = DATA_DIR / "output"
SECRETS_DIR = DATA_DIR / "secrets"
MUSIC_DIR = DATA_DIR / "music"
for _d in (CHARACTER_DIR, CACHE_DIR, OUTPUT_DIR, SECRETS_DIR, MUSIC_DIR):
    _d.mkdir(parents=True, exist_ok=True)

# --- Vertex / Gemini -------------------------------------------------------
# Either point GOOGLE_APPLICATION_CREDENTIALS at the service-account JSON file
# downloaded from the GCP console, or put the JSON *content* in VERTEX_SA_JSON.
MOCK_AI = os.getenv("MOCK_AI", "0") == "1"


def _materialise_sa_json() -> str | None:
    raw = os.getenv("VERTEX_SA_JSON")
    if raw and not os.getenv("GOOGLE_APPLICATION_CREDENTIALS"):
        fd, path = tempfile.mkstemp(suffix=".json")
        with os.fdopen(fd, "w") as fh:
            fh.write(raw)
        os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = path
    return os.getenv("GOOGLE_APPLICATION_CREDENTIALS")


SA_JSON_PATH = _materialise_sa_json()


def _project_from_sa() -> str | None:
    if SA_JSON_PATH and Path(SA_JSON_PATH).exists():
        try:
            return json.loads(Path(SA_JSON_PATH).read_text()).get("project_id")
        except (OSError, ValueError):
            return None
    return None


GCP_PROJECT = os.getenv("GOOGLE_CLOUD_PROJECT") or _project_from_sa()
GCP_LOCATION = os.getenv("GOOGLE_CLOUD_LOCATION", "us-central1")

TEXT_MODEL = os.getenv("TEXT_MODEL", "gemini-2.5-flash")
IMAGE_MODEL = os.getenv("IMAGE_MODEL", "gemini-2.5-flash-image")
VEO_MODEL = os.getenv("VEO_MODEL", "veo-3.1-fast-generate-001")
TTS_VOICES = {
    "en": os.getenv("TTS_VOICE_EN", "en-IN-Neural2-A"),
    "hi": os.getenv("TTS_VOICE_HI", "hi-IN-Neural2-A"),
}
TTS_LANG_CODES = {"en": "en-IN", "hi": "hi-IN"}

# --- Video defaults ---------------------------------------------------------
VIDEO_W = int(os.getenv("VIDEO_W", "1280"))
VIDEO_H = int(os.getenv("VIDEO_H", "720"))
FPS = int(os.getenv("FPS", "24"))
CROSSFADE_SEC = float(os.getenv("CROSSFADE_SEC", "0.8"))
TARGET_SECONDS = int(os.getenv("TARGET_SECONDS", "120"))
VEO_CLIP_SECONDS = 8  # fixed by the Veo API
SUBTITLE_FONT = os.getenv("SUBTITLE_FONT")  # path to a .ttf with Devanagari glyphs, optional

# --- Pricing (USD) ----------------------------------------------------------
USD_TO_INR = float(os.getenv("USD_TO_INR", "84"))
PRICES = {
    # per 1M tokens
    "text_input_per_1m": 0.30,
    "text_output_per_1m": 2.50,
    # per image
    "image_per_unit": 0.039,
    # per second of generated video
    "veo_per_second": 0.15,
    # per 1M characters
    "tts_per_1m_chars": 16.0,
}
for _k in list(PRICES):
    _env = os.getenv("PRICE_" + _k.upper())
    if _env:
        PRICES[_k] = float(_env)

# --- YouTube ----------------------------------------------------------------
YOUTUBE_CLIENT_SECRETS = Path(os.getenv("YOUTUBE_CLIENT_SECRETS", SECRETS_DIR / "youtube_client_secret.json"))
YOUTUBE_TOKEN = SECRETS_DIR / "youtube_token.json"
YOUTUBE_REDIRECT = os.getenv("YOUTUBE_REDIRECT_URI", "http://localhost:8000/youtube/oauth2callback")
YOUTUBE_PRIVACY = os.getenv("YOUTUBE_PRIVACY", "private")  # private | unlisted | public
