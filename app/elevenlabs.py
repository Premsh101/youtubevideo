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
import re
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


def chunk_seconds(durations: list[float], xfade: float = 0.0) -> list[float]:
    """What each song section really lasts: scene lengths include the cross-fade overlap with the next scene,
    a song section must not (otherwise every planned line start drifts one cross-fade later per scene)."""
    secs = [max(3.0, min(120.0, d - xfade)) for d in durations[:-1]] + [max(3.0, min(120.0, durations[-1]))]
    return [round(x, 3) for x in secs]


def build_plan(scenes: list[dict], durations: list[float], lang: str, mode: str, voice: str = "female",
               mood: str = "playful", xfade: float = 0.0) -> dict:
    key = f"line_{lang}"
    md = moods.get(mood)
    pos, neg = _styles(lang, mode, voice, mood)
    chunks = []
    for s, d in zip(scenes, chunk_seconds(durations, xfade)):
        chorus = bool(s.get("is_chorus")) and md["section"] == "Verse"   # lullabies have no shouted chorus
        label = "Chorus" if chorus else md["section"]
        cue = f" {{{md['cue']}}}" if md["cue"] else ""     # {curly braces} = a direction to the singer, not a lyric
        chunks.append({
            "text": f"[{label}]{cue}\n{s[key]}",
            "duration_ms": int(max(3000, min(120000, round(d * 1000)))),
            "positive_styles": pos + (md["chorus_extra"] if chorus else []),
            "negative_styles": neg,
            "context_adherence": "high",
        })
    return {"chunks": chunks}


def compose(scenes: list[dict], durations: list[float], lang: str, mode: str, ledger: CostLedger,
            voice: str = "female", take: int = 0, mood: str = "playful", attempt: int = 0, xfade: float = 0.0) -> Path:
    """One song. `take` is the owner's "new take" counter; `attempt` is our own retry when the singer changed the words."""
    plan = build_plan(scenes, durations, lang, mode, voice, mood, xfade)
    minutes = sum(c["duration_ms"] for c in plan["chunks"]) / 60000
    key = hashlib.sha256(json.dumps([MODEL, plan] + ([take] if take else []) + ([f"a{attempt}"] if attempt else []),
                                    sort_keys=True, ensure_ascii=False).encode()).hexdigest()[:24]
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


# ------------------------------------------------------------------ did the singer sing OUR words?
# The music model sometimes changes or drops words (more often in Hindi). We listen back with speech-to-text,
# compare with the lyrics, and (1) re-record once if too much differs, (2) if it still differs, put what is
# actually sung on screen - so the words on screen always match what children hear.
STT_MODEL = os.getenv("ELEVENLABS_STT_MODEL", "scribe_v2")
MATCH_OK = 0.8          # share of lyric words heard in the song; below this the song is re-recorded once

_DEV_FIX = str.maketrans({"\u093c": "", "\u0901": "\u0902", "\u0903": ""})   # nukta off, chandrabindu -> anusvara


def _tokens(text: str) -> list[str]:
    t = (text or "").lower().translate(_DEV_FIX)
    t = re.sub(r"[^\w\u0900-\u0DFF\u0600-\u06FF\u3040-\u30FF]+", " ", t)
    return [w for w in t.split() if w]


def transcribe(song: Path, lang: str, ledger: CostLedger) -> dict:
    """Words with start times, cached next to the song. {"words": [...]} or {"unavailable": reason}."""
    cache = song.with_suffix(".stt.json")
    if cache.exists():
        data = json.loads(cache.read_text())
        ledger.stt(f"{lang} lyric check", song_duration(song) / 60, cached=True)
        return data
    if config.MOCK_AI:
        from .mock import mock_transcribe
        data = mock_transcribe(song)
    else:
        api_key = os.getenv("ELEVENLABS_API_KEY")
        if not api_key:
            return {"unavailable": "no ElevenLabs key"}
        data = None
        for model in (STT_MODEL, "scribe_v1"):
            def _post(model=model):
                with httpx.Client(timeout=600) as http:
                    r = http.post(f"{API}/v1/speech-to-text", headers={"xi-api-key": api_key},
                                  files={"file": (song.name, song.read_bytes(), "audio/mpeg")},
                                  data={"model_id": model, "language_code": languages.tts_locale(lang).split("-")[0],
                                        "timestamps_granularity": "word", "tag_audio_events": "false", "diarize": "false"})
                if r.status_code in (429, 503):
                    err = RuntimeError(f"ElevenLabs speech-to-text {r.status_code}")
                    err.status_code = r.status_code
                    raise err
                return r
            r = retry.guarded("tts", _post)
            if r.status_code in (401, 403):
                return {"unavailable": "the ElevenLabs key has no Speech to Text permission"}
            if r.status_code >= 400 and "model" in r.text.lower() and model != "scribe_v1":
                continue                                     # this account doesn't have that model: try the older one
            if r.status_code >= 400:
                return {"unavailable": f"speech-to-text error {r.status_code}: {r.text[:120]}"}
            words = [{"text": w["text"], "start": float(w["start"]), "end": float(w.get("end", w["start"]))}
                     for w in r.json().get("words", []) if w.get("type", "word") == "word" and w.get("start") is not None]
            data = {"words": words, "model": model}
            break
        if data is None:
            return {"unavailable": "speech-to-text model not available"}
    ledger.stt(f"{lang} lyric check", song_duration(song) / 60)
    cache.write_text(json.dumps(data, ensure_ascii=False))
    return data


def match_lyrics(words: list[dict], lines: list[str], planned_starts: list[float], total: float) -> dict:
    """Align the heard words to the lyric lines (sequence alignment on normalised words).
    Returns match 0..1 overall, per line, what was sung in each line's slot, and each line's measured start."""
    import difflib
    script, owner = [], []
    for i, line in enumerate(lines):
        for w in _tokens(line):
            script.append(w)
            owner.append(i)
    heard = [(_tokens(w["text"]), w) for w in words]
    heard = [(t[0], w) for t, w in heard if t]
    htok = [t for t, _ in heard]
    sm = difflib.SequenceMatcher(None, script, htok, autojunk=False)
    hit = [0] * len(lines)
    first_hit: list[float | None] = [None] * len(lines)
    last_end: list[float | None] = [None] * len(lines)
    for a, b, size in sm.get_matching_blocks():
        for k in range(size):
            i = owner[a + k]
            hit[i] += 1
            w = heard[b + k][1]
            if first_hit[i] is None or w["start"] < first_hit[i]:
                first_hit[i] = w["start"]
            if last_end[i] is None or w["end"] > last_end[i]:
                last_end[i] = w["end"]
    counts = [max(1, owner.count(i)) for i in range(len(lines))]
    per_line = [round(hit[i] / counts[i], 2) for i in range(len(lines))]
    match = round(sum(hit) / max(1, len(script)), 3)
    # what is sung in each line's slot: between this line's first heard word and the next line's
    starts = []
    for i in range(len(lines)):
        if first_hit[i] is not None:
            starts.append(first_hit[i])
        elif i > 0 and last_end[i - 1] is not None:      # not heard: its slot begins where the previous line ended
            starts.append(last_end[i - 1] + 0.05)
        else:
            starts.append(planned_starts[i])
    for i in range(1, len(starts)):                       # keep the order sane when a line was not heard at all
        starts[i] = max(starts[i], starts[i - 1] + 1.0)
    sung = []
    for i in range(len(lines)):
        lo, hi = starts[i] - 0.15, (starts[i + 1] - 0.15) if i + 1 < len(lines) else total + 1
        sung.append(" ".join(w["text"] for _, w in heard if lo <= w["start"] < hi).strip())
    return {"match": match, "per_line": per_line, "sung": sung,
            "starts": [round(s, 3) for s in starts], "heard": [first_hit[i] is not None for i in range(len(lines))]}


def verify(song: Path, scenes: list[dict], lang: str, planned_starts: list[float], ledger: CostLedger) -> dict:
    data = transcribe(song, lang, ledger)
    if "words" not in data:
        return {"available": False, "reason": data.get("unavailable", "unknown")}
    lines = [s[f"line_{lang}"] for s in scenes]
    res = match_lyrics(data["words"], lines, planned_starts, song_duration(song))
    return {"available": True, **res}
