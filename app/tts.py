"""Voice-over via Google Cloud Text-to-Speech (same service-account credentials).

Each lyric line is synthesised separately so the renderer knows exactly how long
every scene's narration is.  Results are cached by (voice, text, prosody).
"""
from __future__ import annotations

import hashlib
import json
import math
import struct
import subprocess
import wave
from pathlib import Path

from . import config, languages, retry, toddler
from .costs import CostLedger

_tts_client = None


def _client():
    global _tts_client
    if _tts_client is None:
        from google.cloud import texttospeech
        _tts_client = texttospeech.TextToSpeechClient()
    return _tts_client


def media_duration(path: Path) -> float:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "json", str(path)],
        capture_output=True, text=True, check=True,
    ).stdout
    return float(json.loads(out)["format"]["duration"])


def _ssml(text: str, chorus: bool = False) -> str:
    """Sing-song delivery: chorus lines brighter/higher like a sung hook, verses a bit slower."""
    v = toddler.VOICE
    safe = text.replace("&", "&amp;").replace("<", "&lt;")
    pitch = v["pitch_semitones"] + (2.0 if chorus else 0.0)
    rate = v["speaking_rate"] + (0.06 if chorus else 0.0)
    return (f'<speak><prosody rate="{int(rate * 100)}%" pitch="+{pitch:.1f}st">'
            f'{safe}</prosody></speak>')


def synthesize_line(text: str, lang: str, ledger: CostLedger, chorus: bool = False, gender: str = "female") -> Path:
    male = gender == "male"
    voice = None if male else languages.tts_voice(lang)
    key = hashlib.sha256(f"{lang}|{voice}|{gender if male else ''}|{json.dumps(toddler.VOICE, sort_keys=True)}|{int(chorus)}|{text}".encode()).hexdigest()[:24]
    out = config.CACHE_DIR / "tts" / f"{key}.wav"
    if out.exists():
        ledger.tts(f"{lang} line", len(text), cached=True)
        return out
    out.parent.mkdir(parents=True, exist_ok=True)

    if config.MOCK_AI:
        _mock_voice(text, out)
        ledger.tts(f"{lang} line", len(text))
        return out

    from google.cloud import texttospeech as t
    resp = retry.guarded("tts", lambda: _client().synthesize_speech(
        input=t.SynthesisInput(ssml=_ssml(text, chorus)),
        voice=(t.VoiceSelectionParams(language_code=languages.tts_locale(lang), name=voice) if voice else
               t.VoiceSelectionParams(language_code=languages.tts_locale(lang), ssml_gender=t.SsmlVoiceGender.FEMALE)),
        audio_config=t.AudioConfig(audio_encoding=t.AudioEncoding.LINEAR16, sample_rate_hertz=24000),
    ))
    out.write_bytes(resp.audio_content)
    ledger.tts(f"{lang} line", len(text))
    return out


def _mock_voice(text: str, out: Path) -> None:
    """Hummed syllables (one soft tone per word) so timing resembles real speech."""
    sr = 24000
    words = max(1, len(text.split()))
    per = 0.42
    frames = bytearray()
    for w in range(words):
        f = 220 + 40 * ((w * 7) % 5)
        n = int(sr * per)
        for i in range(n):
            env = math.sin(math.pi * i / n) ** 2
            frames += struct.pack("<h", int(6000 * env * math.sin(2 * math.pi * f * i / sr)))
    with wave.open(str(out), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sr)
        wf.writeframes(bytes(frames))


def trimmed(path: Path) -> Path:
    """Strip the silence TTS adds before/after speech, so a line starts exactly where we place it."""
    out = path.with_suffix(".trim.wav")
    if not out.exists():
        sil = "silenceremove=start_periods=1:start_threshold=-45dB:start_silence=0.03"
        subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", str(path), "-af",
                        f"{sil},areverse,{sil},areverse", str(out)], check=True)
        if media_duration(out) < 0.2:  # never trim a line away entirely
            out.write_bytes(path.read_bytes())
    return out


def synthesize_scenes(scenes: list[dict], lang: str, ledger: CostLedger, voice: str = "female") -> list[dict]:
    """Return [{path, duration}] aligned with scenes."""
    key = f"line_{lang}"
    out = []
    for s in scenes:
        p = trimmed(synthesize_line(s[key], lang, ledger, chorus=bool(s.get("is_chorus")), gender=voice))
        out.append({"path": str(p), "duration": media_duration(p), "text": s[key], "chorus": bool(s.get("is_chorus"))})
    return out
