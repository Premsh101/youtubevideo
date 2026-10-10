"""Mood of a video: one setting that drives the music, the voice, the lyrics and the look together.

A sleepy lullaby must not get party drums, so every place that used to have a fixed "cheerful 92 bpm" style now asks
here: ElevenLabs style words and tempo, the Google voice speed/pitch, the synthesised background music, the lyric
style Gemini is asked for, and the picture colours.  "auto" lets Gemini choose from the topic (see `guess`).
"""
from __future__ import annotations

import re

DEFAULT = "playful"

MOODS: dict[str, dict] = {
    "sleepy": {
        "label": "😴 Sleepy lullaby", "bpm": 60,
        "rate": 0.74, "pitch": 0.0, "chorus_rate": 0.0, "chorus_pitch": 0.0, "volume_db": -12,
        "melody_notes": 2, "chorus_extra": [],
        "section": "Lullaby", "cue": "(sung very softly and slowly, like a whispered lullaby)",
        "pos": ["slow gentle lullaby", "music box", "soft harp", "warm string pads", "gentle humming", "solo soft voice",
                "very calm and sleepy", "dreamy", "minor-free simple melody"],
        "neg": ["drums", "percussion", "claps", "shouting", "chorus singing along", "upbeat", "disco", "bright", "fast",
                "energetic", "dance", "electronic beats"],
        "lyrics": "very soft, slow and sleepy: stars, moon, blankets, yawns, dreams; whispery sounds; NO loud actions or exclamations",
        "visual": "night-time, soft moonlit blues and warm lamp glow, very calm, dreamy and sleepy, gentle light",
        "max_onsets": 1.6,
    },
    "calm": {
        "label": "🌿 Calm & gentle", "bpm": 76,
        "rate": 0.82, "pitch": 1.0, "chorus_rate": 0.02, "chorus_pitch": 0.5, "volume_db": -14,
        "melody_notes": 3, "chorus_extra": [],
        "section": "Verse", "cue": "(sung gently and warmly)",
        "pos": ["gentle children's song", "soft acoustic guitar", "light piano", "soft shaker", "warm friendly voice",
                "relaxed and peaceful", "simple catchy melody"],
        "neg": ["heavy drums", "loud", "aggressive", "disco", "fast", "electronic beats", "shouting"],
        "lyrics": "gentle and soothing, slow flowing sounds, peaceful nature and cosy moments",
        "visual": "soft pastel colours, peaceful, relaxed light",
        "max_onsets": 2.4,
    },
    "playful": {
        "label": "🎈 Playful", "bpm": 92,
        "rate": 0.88, "pitch": 2.5, "chorus_rate": 0.06, "chorus_pitch": 2.0, "volume_db": -16,
        "melody_notes": 4, "chorus_extra": ["energetic hook", "kids singing along loudly"],
        "section": "Verse", "cue": "",
        "pos": ["cheerful female lead vocal", "kids chorus sing-along", "glockenspiel, ukulele, soft claps",
                "simple catchy melody", "preschool TV theme", "warm and gentle", "major key"],
        "neg": ["distorted", "aggressive", "scary", "fast rap", "heavy drums", "dissonant", "adult themes", "silence"],
        "lyrics": "happy, bouncy and playful with actions and sounds",
        "visual": "", "max_onsets": None,
    },
    "energetic": {
        "label": "🕺 Energetic dance", "bpm": 108,
        "rate": 0.94, "pitch": 3.0, "chorus_rate": 0.06, "chorus_pitch": 2.0, "volume_db": -16,
        "melody_notes": 4, "chorus_extra": ["energetic hook", "kids singing along loudly", "clapping"],
        "section": "Verse", "cue": "(sung brightly and bouncily)",
        "pos": ["upbeat children's dance song", "claps and stomps", "bouncy ukulele", "kids chorus sing-along",
                "joyful and lively", "major key", "simple catchy melody"],
        "neg": ["sleepy", "slow", "dark", "aggressive", "distorted", "scary", "adult themes"],
        "lyrics": "lively, full of actions to copy (clap, jump, spin, stomp) and happy exclamations",
        "visual": "bright, saturated, cheerful colours", "max_onsets": None,
    },
}

_SLEEPY = re.compile(r"\b(sleep\w*|lullaby|lullabies|bed\s?time|bedtime|good\s?night|night\s?night|nap\w*|dream\w*|moon\w*|"
                     r"twinkle|rock[- ]a[- ]bye|hush|cradle)\b", re.I)
_CALM = re.compile(r"\b(calm|gentle|rain|nature|garden|butterfl\w*|story|peace\w*|relax\w*|bath|breath\w*)\b", re.I)
_LIVELY = re.compile(r"\b(dance|jump|party|hop|clap|stomp|march|run|race|wheels|bus|train|disco)\b", re.I)


def ids() -> list[str]:
    return list(MOODS)


def get(mood: str | None) -> dict:
    return MOODS.get(mood or "", MOODS[DEFAULT]) | {"id": mood if mood in MOODS else DEFAULT}


def guess(text: str) -> str:
    """Cheap keyword fallback when Gemini did not say (or said something unknown)."""
    if _SLEEPY.search(text or ""):
        return "sleepy"
    if _CALM.search(text or ""):
        return "calm"
    if _LIVELY.search(text or ""):
        return "energetic"
    return DEFAULT


def resolve(requested: str | None, from_script: str | None, text: str = "") -> str:
    """User's explicit choice wins; "auto"/empty uses Gemini's pick, then a keyword guess."""
    if requested in MOODS:
        return requested
    return from_script if from_script in MOODS else guess(text)
