"""Poem / rhyme + scene plan generation with Gemini.

One call produces BOTH the English and Hindi lyric for every scene, so a single
set of visuals serves both language versions.  The user may paste their own
poem (any language) — Gemini then only translates/splits it into scenes.
"""
from __future__ import annotations

from . import config, toddler
from .costs import CostLedger
from .gemini_client import generate_json

SECONDS_PER_SCENE_IMAGES = 7    # top channels change picture every ~5-8 s; slower than that loses toddlers
SECONDS_PER_SCENE_VEO = config.VEO_CLIP_SECONDS


def scene_count(engine: str, target_seconds: int) -> int:
    per = SECONDS_PER_SCENE_VEO if engine == "veo" else SECONDS_PER_SCENE_IMAGES
    return max(6, min(24, round(target_seconds / per)))


def build_prompt(topic: str | None, user_poem: str | None, characters: list[dict],
                 mode: str, n_scenes: int) -> str:
    char_lines = "\n".join(f"- {c['name']} the {c['species']} ({c['personality']}, {' & '.join(c['colours'])}, wears {c['signature_item']})"
                           for c in characters)
    source = (
        f'Use THIS poem/rhyme written by the user as the lyrics (keep its meaning, you may lightly smooth rhythm):\n"""\n{user_poem}\n"""\n'
        if user_poem else
        f'Write an ORIGINAL rhyme about: "{topic or "a happy day with friends"}".'
    )
    return f"""You write nursery rhymes for toddlers aged 1-3 for a bilingual (English + Hindi) YouTube channel.

{source}

Characters that MUST appear (same look in every scene):
{char_lines}

Toddler rules (this is what the most-watched channels do): very simple words, LOTS of repetition,
4-8 words per line, strong sing-song rhythm, onomatopoeia and actions (clap, splash, beep, quack),
a short catchy HOOK line that repeats as a chorus at least 3 times across the video, and a happy
calm ending. Mark chorus lines with "is_chorus": true (hook must be word-for-word identical each time). Hindi must be natural spoken Hindi in Devanagari (not a literal translation);
both versions must match the same scene meaning. No scary, sad or violent content.

Produce exactly {n_scenes} scenes. Each scene = one lyric line (EN + HI) + one picture.
The pictures must look like ONE continuous story in a {mode.upper()} animated style:
same location palette, time of day evolving slowly, characters in consistent outfits.
For "visual" write a concrete, static, uncluttered composition (who, where, doing what,
foreground/background), 25-45 words, NO text in the image.
"camera" is one of: slow zoom in, slow zoom out, gentle pan left, gentle pan right.
"mood_colour" is one of: {', '.join(k.replace('_', ' ') for k in toddler.PALETTE)}.

Return JSON:
{{
 "title_en": str, "title_hi": str,
 "description_en": str (2 sentences), "description_hi": str,
 "tags": [8 short tags, mixed EN/HI],
 "scenes": [{{"index": 0, "line_en": str, "line_hi": str, "is_chorus": bool, "visual": str, "camera": str, "mood_colour": str}}]
}}"""


def generate_script(topic: str | None, user_poem: str | None, characters: list[dict],
                    mode: str, engine: str, target_seconds: int, ledger: CostLedger) -> dict:
    n = scene_count(engine, target_seconds)
    prompt = build_prompt(topic, user_poem, characters, mode, n)
    data = generate_json(prompt, ledger, "script + scenes")
    scenes = data.get("scenes") or []
    if not scenes:
        raise RuntimeError("Gemini returned no scenes")
    for i, s in enumerate(scenes):
        s["index"] = i
        s.setdefault("camera", ["slow zoom in", "gentle pan right", "slow zoom out", "gentle pan left"][i % 4])
        s.setdefault("mood_colour", "sky blue")
        s["is_chorus"] = bool(s.get("is_chorus"))
    data["scenes"] = scenes
    data["mode"] = mode
    data["engine"] = engine
    return data
