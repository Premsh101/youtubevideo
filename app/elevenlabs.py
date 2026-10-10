"""Optional *sung* vocals via the ElevenLabs Music API (the big channels use real singing).

We hand the API a composition plan: one chunk per scene with that scene's lyric line and the
scene's duration, so we know roughly where each line lands in the song.  The actual song
length is probed afterwards and scene starts are scaled to match.  One song per language;
the video track is still shared.  Needs ELEVENLABS_API_KEY; cached by plan hash.
"""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
from pathlib import Path

import httpx

from . import config, languages, moods, retry
from .costs import CostLedger

API = os.getenv("ELEVENLABS_API", "https://api.elevenlabs.io")
MODEL = os.getenv("ELEVENLABS_MUSIC_MODEL", "music_v2_5")


def is_configured() -> bool:
    return bool(os.getenv("ELEVENLABS_API_KEY")) or config.MOCK_AI


def _styles(lang: str, mode: str, voice: str = "female", mood: str = "playful") -> tuple[list[str], list[str]]:
    """What the song should sound like. The mood (sleepy ... energetic) sets tempo, instruments and what to avoid."""
    md = moods.get(mood)
    lead = "friendly male lead vocal" if voice == "male" else "cheerful female lead vocal"
    pos = [f"children's nursery rhyme sung in {languages.name(lang)}", f"{md['bpm']} bpm", "clear pronunciation"]
    for w in md["pos"]:
        # the mood's own list names a lead voice for playful only; keep the chosen voice for every mood
        if w == "cheerful female lead vocal":
            w = lead
        pos.append(w)
    if md["id"] in ("sleepy", "calm"):
        pos.append("soft warm male voice" if voice == "male" else "soft warm female voice")
    elif lead not in pos:            # a mood whose style list names no lead voice still gets the chosen one
        pos.append(lead)
    return pos, list(md["neg"])


def build_plan(scenes: list[dict], durations: list[float], lang: str, mode: str, voice: str = "female",
               mood: str = "playful") -> dict:
    key = f"line_{lang}"
    md = moods.get(mood)
    pos, neg = _styles(lang, mode, voice, mood)
    chunks = []
    for s, d in zip(scenes, durations):
        chorus = bool(s.get("is_chorus")) and md["section"] == "Verse"   # lullabies have no shouted chorus
        label = "Chorus" if chorus else md["section"]
        cue = f"\n{md['cue']}" if md["cue"] else ""
        chunks.append({
            "text": f"[{label}]{cue}\n{s[key]}",
            "duration_ms": int(max(3000, min(120000, round(d * 1000)))),
            "positive_styles": pos + (md["chorus_extra"] if chorus else []),
            "negative_styles": neg,
            "context_adherence": "high",
        })
    return {"chunks": chunks}


def compose(scenes: list[dict], durations: list[float], lang: str, mode: str, ledger: CostLedger,
            voice: str = "female", take: int = 0, mood: str = "playful") -> Path:
    plan = build_plan(scenes, durations, lang, mode, voice, mood)
    minutes = sum(c["duration_ms"] for c in plan["chunks"]) / 60000
    key = hashlib.sha256(json.dumps([MODEL, plan] + ([take] if take else []), sort_keys=True, ensure_ascii=False).encode()).hexdigest()[:24]
    out = config.CACHE_DIR / "songs" / f"{key}.mp3"
    if out.exists():
        ledger.sung(f"{lang} song", minutes, cached=True)
        return out
    out.parent.mkdir(parents=True, exist_ok=True)

    if config.MOCK_AI:
        from .mock import mock_song
        mock_song(scenes, durations, lang, out)
        ledger.sung(f"{lang} song", minutes)
        return out

    api_key = os.getenv("ELEVENLABS_API_KEY")
    if not api_key:
        raise RuntimeError("ELEVENLABS_API_KEY is not set (needed for sung vocals)")
    def _post():
        with httpx.Client(timeout=600) as http:
            r = http.post(f"{API}/v1/music", params={"output_format": "mp3_44100_128"},
                          headers={"xi-api-key": api_key, "Content-Type": "application/json"},
                          json={"model_id": MODEL, "composition_plan": plan, "respect_sections_durations": True})
        if r.status_code >= 400:
            err = RuntimeError(f"ElevenLabs music error {r.status_code}: {r.text[:500]}")
            err.status_code = r.status_code  # lets retry.py recognise 429/503
            raise err
        return r
    r = retry.guarded("tts", _post)
    out.write_bytes(r.content)
    ledger.sung(f"{lang} song", minutes)
    return out


def song_duration(path: Path) -> float:
    o = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "json", str(path)],
                       capture_output=True, text=True, check=True).stdout
    return float(json.loads(o)["format"]["duration"])
