"""Poem / rhyme + scene plan generation with Gemini.

One call produces BOTH the English and Hindi lyric for every scene, so a single
set of visuals serves both language versions.  The user may paste their own
poem (any language) — Gemini then only translates/splits it into scenes.
"""
from __future__ import annotations

from . import config, cutout, lyrics, moods, toddler
from .costs import CostLedger
from .gemini_client import generate_json

SECONDS_PER_SCENE_IMAGES = 7    # top channels change picture every ~5-8 s; slower than that loses toddlers
SECONDS_PER_SCENE_VEO = config.VEO_CLIP_SECONDS


def scene_count(engine: str, target_seconds: int) -> int:
    per = SECONDS_PER_SCENE_VEO if engine == "veo" else SECONDS_PER_SCENE_IMAGES
    return max(6, min(24, round(target_seconds / per)))


def build_prompt(topic: str | None, user_poem: str | None, characters: list[dict],
                 mode: str, n_scenes: int, engine: str = "images", mood: str | None = None) -> str:
    char_lines = "\n".join(f"- {c['name']} the {c['species']} ({c['personality']}, {' & '.join(c['colours'])}, wears {c['signature_item']})"
                           for c in characters)
    source = (
        f'Use THIS poem/rhyme written by the user as the lyrics (keep its meaning, you may lightly smooth rhythm):\n"""\n{user_poem}\n"""\n'
        if user_poem else
        topic_block(topic)
    )
    lead = lyrics.detect_language(user_poem or topic or "") or "en"
    cutout_block = cutout.SCRIPT_BLOCK.format(keys=", ".join(cutout.LOCATIONS)) if engine == "cutout" else ""
    cutout_fields = cutout.SCRIPT_FIELDS if engine == "cutout" else ""
    mood_block = mood_text(mood)
    return f"""You write nursery rhymes for toddlers aged 1-3 for a bilingual (English + Hindi) YouTube channel.

{source}

Characters that MUST appear (same look in every scene):
{char_lines}

{lead_block(lead)}
Toddler rules (this is what the most-watched channels do): very simple words, LOTS of repetition,
4-8 words per line, strong sing-song rhythm, RHYMING COUPLETS (lines 1+2, 3+4, 5+6 ... must end with rhyming words), onomatopoeia and actions (clap, splash, beep, quack),
a short catchy HOOK line that repeats as a chorus at least 3 times across the video, and a happy
calm ending. Mark chorus lines with "is_chorus": true (hook must be word-for-word identical each time). Hindi must be natural spoken Hindi in Devanagari (not a literal translation);
both versions must match the same scene meaning. No scary, sad or violent content.
{lyrics.HINDI_RULES}

Produce exactly {n_scenes} scenes. Each scene = one lyric line (EN + HI) + one picture.
The pictures must look like ONE continuous story in a {mode.upper()} animated style:
same location palette, time of day evolving slowly, characters in consistent outfits.
For "visual" write a concrete, static, uncluttered composition (who, where, doing what,
foreground/background), 25-45 words, NO text in the image.
"camera" is one of: slow zoom in, slow zoom out, gentle pan left, gentle pan right.
"mood_colour" is one of: {', '.join(k.replace('_', ' ') for k in toddler.PALETTE)}.
{cutout_block}
{mood_block}
Return JSON:
{{
 "title_en": str, "title_hi": str, "mood": "sleepy | calm | playful | energetic",
 "known_rhyme": str or null, "copyrighted": bool,
 "description_en": str (2 sentences), "description_hi": str,
 "tags": [8 short tags, mixed EN/HI],
 "scenes": [{{"index": 0, "line_en": str, "line_hi": str, "is_chorus": bool, "visual": str, "camera": str, "mood_colour": str,{cutout_fields}}}]
}}"""


def topic_block(topic: str | None) -> str:
    """The theme the owner typed. It may be the NAME of a rhyme they want (classic: use it; copyrighted: don't copy)."""
    theme = topic or "a happy day with friends"
    return f"""The owner typed this theme: "{theme}".
If it is the title or first line of a WELL-KNOWN rhyme (e.g. "Machli jal ki rani", "Twinkle twinkle"):
- traditional / public-domain rhyme: the owner wants THAT rhyme, so use its real, well-known words in its own language
  and native script, and continue it faithfully (extend with verses in the same spirit if more lines are needed);
  set "known_rhyme" to its name and "copyrighted" to false.
- copyrighted film or pop song (e.g. "Lakdi ki kathi" from the film Masoom, "Baby Shark"): do NOT reproduce its lyrics
  or melody. Write a NEW rhyme on the same subject with a similar bounce; set "known_rhyme" to its name and
  "copyrighted" to true.
Otherwise write an ORIGINAL rhyme about the theme and set "known_rhyme" to null."""


def lead_block(lead: str) -> str:
    if lead == "hi":
        return ("The owner wrote in HINDI (Devanagari or romanised). Write the HINDI lines FIRST as the real song - natural "
                "बाल-गीत in Devanagari, rhyming couplets - then write the English lines as their own rhyming song with the "
                "same meaning. Hindi is the original here, English the version.")
    return ""


MOOD_QUESTION = ('"mood" is the feel of the whole video: sleepy (lullaby, bedtime), calm (gentle, peaceful), playful (default) or '
                 'energetic (dance, action). It decides the music and voice, so pick it from the theme.')


def mood_text(mood: str | None) -> str:
    """Prompt text: the owner's chosen mood (lyrics must match it), or ask Gemini to pick one."""
    if mood in moods.MOODS:
        return (f"Owner's chosen mood: {mood}. The lyrics must be {moods.MOODS[mood]['lyrics']}. "
                f"The pictures: {moods.MOODS[mood]['visual'] or 'bright and cheerful'}. Set \"mood\" to \"{mood}\".")
    return MOOD_QUESTION


def _fix_mood(data: dict, requested: str | None, topic: str | None) -> None:
    text = " ".join([topic or "", data.get("title_en") or "", data.get("about") or ""])
    data["mood"] = moods.resolve(requested, data.get("mood"), text)
    data["lead_lang"] = lyrics.detect_language(topic or "") or "en"
    kr = data.get("known_rhyme")
    data["known_rhyme"] = str(kr).strip() if kr and str(kr).strip().lower() not in ("null", "none", "") else None
    data["copyrighted"] = bool(data.get("copyrighted")) and data["known_rhyme"] is not None


def generate_clip_script(segments: list[dict], topic: str | None, user_poem: str | None, ledger: CostLedger,
                         mood: str | None = None) -> dict:
    """Lyrics for footage that already exists: one rhyming line per segment, matching what that segment shows.
    `segments` carry a "describe" text (from Gemini's watching of the clip). Same result shape as generate_script,
    so the rest of the pipeline (rhyming per language, voice, thumbnails, publishing) is shared."""
    n = len(segments)
    board = "\n".join(f'{i + 1}. ({s["duration"]:.0f} s) {s["describe"]}' for i, s in enumerate(segments))
    source = (f'Use THIS poem/rhyme written by the owner as the lyrics, matching its lines to the segments by meaning '
              f'(keep its words; you may lightly smooth the rhythm):\n"""\n{user_poem}\n"""'
              if user_poem else
              f'Write ORIGINAL lyrics that describe what each segment shows{f", on the theme: {topic}" if topic else ""}.')
    lead = lyrics.detect_language(user_poem or topic or "") or "en"
    prompt = f"""CLIP SCRIPT. You write nursery rhymes for toddlers aged 1-3 for a bilingual (English + Hindi) YouTube channel.
{lead_block(lead)}
The pictures ALREADY EXIST: the video is composed of {n} segments, shown in this order. Write lyrics that FIT what each
segment shows: exactly one lyric line per segment, sung while that segment is on screen, naming the visible things and
actions in simple words.

SEGMENTS:
{board}

{source}

{mood_text(mood)}
Rules: RHYMING COUPLETS (lines 1+2, 3+4, 5+6 ... must end with rhyming words), 4-9 very simple words per line, repetition,
a short catchy HOOK line repeated as a chorus at least twice ("is_chorus": true, word for word identical each time) if
there are 6 or more segments, a happy calm ending. Never describe anything that is not visible in the segment.
No scary, sad or violent wording. Hindi: natural spoken Hindi in Devanagari, not a literal translation.
{lyrics.HINDI_RULES}

Return JSON: {{"title_en": str, "title_hi": str, "mood": "sleepy | calm | playful | energetic", "description_en": str (2 sentences), "description_hi": str,
"tags": [8 short tags, mixed EN/HI], "about": "one sentence: what the whole video shows",
"scenes": [{{"index": 0, "line_en": str, "line_hi": str, "is_chorus": bool, "visual": "short description of the segment"}}]}}"""
    data = generate_json(prompt, ledger, "lyrics for your clips")
    scenes = data.get("scenes") or []
    if len(scenes) != n:
        raise RuntimeError(f"Gemini returned {len(scenes)} lines for {n} clip segments; press Resume to try again")
    for i, s in enumerate(scenes):
        s["index"] = i
        s["is_chorus"] = bool(s.get("is_chorus"))
        s.setdefault("visual", segments[i]["describe"][:120])
        s.setdefault("camera", "slow zoom in")
        s.setdefault("mood_colour", "sky blue")
    data["scenes"] = scenes
    data["mode"], data["engine"] = "2d", "clips"
    _fix_mood(data, mood, topic)
    return data


def generate_script(topic: str | None, user_poem: str | None, characters: list[dict],
                    mode: str, engine: str, target_seconds: int, ledger: CostLedger, mood: str | None = None) -> dict:
    n = scene_count(engine, target_seconds)
    prompt = build_prompt(topic, user_poem, characters, mode, n, engine, mood)
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
    if engine == "cutout":
        cutout.normalize_script(data, characters)
    data["mode"] = mode
    data["engine"] = engine
    _fix_mood(data, mood, topic)
    return data
