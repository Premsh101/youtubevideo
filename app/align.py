"""Find when each lyric line actually starts in a generated song.

ElevenLabs' music models treat section durations as a hint, so the real song drifts from the
plan.  We measure instead of trusting the plan, and the pictures are then cut to these times:
  1. ElevenLabs forced alignment (exact; the API key needs "Forced Alignment" access)
  2. Gemini on Vertex listening to the song (approximate, ~0.3 s)
  3. the composition plan (last resort)
"""
from __future__ import annotations

import json
import os

import httpx

from . import config, retry
from .costs import CostLedger


def _valid(starts: list[float], total: float, n: int) -> bool:
    if len(starts) != n or starts[0] < 0 or starts[-1] >= total - 0.5:
        return False
    return all(b - a >= 1.0 for a, b in zip(starts, starts[1:]))


def _elevenlabs(song: bytes, lines: list[str]) -> list[float] | None:
    key = os.getenv("ELEVENLABS_API_KEY")
    if not key:
        return None
    text = " ".join(lines)

    def _post():
        with httpx.Client(timeout=300) as http:
            r = http.post("https://api.elevenlabs.io/v1/forced-alignment", headers={"xi-api-key": key},
                          files={"file": ("song.mp3", song, "audio/mpeg")}, data={"text": text})
        if r.status_code in (429, 503):
            err = RuntimeError(f"ElevenLabs alignment {r.status_code}")
            err.status_code = r.status_code
            raise err
        return r

    r = retry.guarded("tts", _post)
    if r.status_code >= 400:  # e.g. 401/403 when the key lacks Forced Alignment access
        return None
    chars = r.json().get("characters") or []
    if len(chars) < len(text) - 2:
        return None
    starts, offset = [], 0
    for line in lines:
        starts.append(float(chars[min(offset, len(chars) - 1)]["start"]))
        offset += len(line) + 1
    return starts


def _gemini(song: bytes, lines: list[str], ledger: CostLedger) -> list[float] | None:
    from google.genai import types

    from .gemini_client import _parse_json, client
    numbered = "\n".join(f"{i + 1}. {line}" for i, line in enumerate(lines))
    prompt = (f"Listen to this children's song. For each of these {len(lines)} lyric lines, give the time in "
              f"seconds (decimals) when the singer STARTS singing it.\n{numbered}\n"
              'Return JSON: {"line_starts": [one number per line, in order]}')
    resp = retry.guarded("text", lambda: client().models.generate_content(
        model=config.TEXT_MODEL,
        contents=[types.Part.from_bytes(data=song, mime_type="audio/mpeg"), prompt],
        config=types.GenerateContentConfig(response_mime_type="application/json", temperature=0),
    ))
    u = resp.usage_metadata
    ledger.text("song alignment", u.prompt_token_count or 0, u.candidates_token_count or 0)
    return [float(x) for x in _parse_json(resp.text).get("line_starts", [])]


def line_starts(song_path, lines: list[str], total: float, planned: list[float], ledger: CostLedger) -> tuple[list[float], str]:
    """Return (start time of each line in the song, method used)."""
    cache = song_path.with_suffix(".align.json")
    if cache.exists():
        data = json.loads(cache.read_text())
        return data["starts"], data["method"]
    if config.MOCK_AI:
        truth = song_path.with_suffix(".truth.json")
        starts, method = (json.loads(truth.read_text()), "mock-measured") if truth.exists() else (planned, "plan")
    else:
        song = song_path.read_bytes()
        starts, method = None, "plan"
        for name, fn in (("elevenlabs", lambda: _elevenlabs(song, lines)), ("gemini", lambda: _gemini(song, lines, ledger))):
            try:
                cand = fn()
            except retry.QuotaExhausted:
                raise
            except Exception:  # noqa: BLE001 - alignment is best-effort, fall through to the next method
                cand = None
            if cand and _valid(cand, total, len(lines)):
                starts, method = cand, name
                break
        if starts is None:
            starts = planned
    cache.write_text(json.dumps({"starts": starts, "method": method}))
    return starts, method
