"""Per-language lyrics translation and YouTube metadata written for discovery.

Kept inside YouTube policy on purpose: no other channels' names (Cocomelon, ChuChu…) in tags,
no misleading or unrelated keywords, no tag stuffing in descriptions — those get videos
demoted or removed, which kills reach far more than any keyword gains.
"""
from __future__ import annotations

from . import config, languages
from .costs import CostLedger
from .gemini_client import generate_json

TAGS_CHAR_LIMIT = 480  # YouTube's limit is 500 characters across all tags


def translate_lines(script: dict, lang: str, ledger: CostLedger) -> list[str]:
    """New singable lyric line per scene, aligned 1:1 with the existing pictures."""
    scenes = script["scenes"]
    src = "\n".join(f'{i + 1}. [{"CHORUS" if s.get("is_chorus") else "verse"}] {s["line_en"]}'
                    f'  (scene: {s["visual"][:90]})' for i, s in enumerate(scenes))
    prompt = f"""You adapt toddler nursery rhymes (age 1-3) into {languages.name(lang)}.
Below are exactly {len(scenes)} lines, one per picture of an existing animated video. Write exactly
{len(scenes)} lines in natural, spoken {languages.name(lang)} (native script), one per input line, same meaning
as its picture so it still matches the visuals.
Rules: simple toddler words, rhyme and rhythm in {languages.name(lang)} (adapt, don't translate word for word),
similar length/syllables as the English line so it fits the same time, every CHORUS line must be
word-for-word identical each time it appears, keep sounds like "splash", "quack" as fun local onomatopoeia.

{src}

Return JSON: {{"translated_lines": [exactly {len(scenes)} strings], "title": "short rhyme title in {languages.name(lang)}"}}"""
    data = generate_json(prompt, ledger, f"lyrics {lang}")
    lines = data.get("translated_lines") or []
    if len(lines) != len(scenes):
        raise RuntimeError(f"Gemini returned {len(lines)} {lang} lines for {len(scenes)} scenes; press Retry")
    script.setdefault("titles", {})[lang] = data.get("title")
    return [str(x) for x in lines]


def viral_metadata(script: dict, lang: str, cast: list[dict], ledger: CostLedger) -> dict:
    """Title, description, tags and hashtags optimised for YouTube Kids search & suggested videos."""
    key = f"line_{lang}"
    lyrics = "\n".join(s.get(key, "") for s in script["scenes"])
    base_title = (script.get("titles") or {}).get(lang) or script.get(f"title_{lang}") or script.get("title_en")
    chars = ", ".join(f"{c['name']} the {c['species']}" for c in cast) or "cute animal friends"
    prompt = f"""You are a YouTube growth strategist for "{config.CHANNEL_NAME}", a toddler (age 1-3) nursery-rhyme channel.
Write the metadata for this video in {languages.name(lang)} (native script; add common English/romanised
search terms too where parents search that way, e.g. Hindi parents also type "hindi rhymes", "balgeet").

Rhyme: {base_title}
Characters: {chars}
Lyrics:
{lyrics}

What works for the most-viewed kids videos:
- TITLE (max 70 chars): rhyme name first (exact name parents search), then a hook, then a broad
  category phrase, then the channel name if it fits, e.g.
  "Johny Johny Yes Papa 🍭 | Nursery Rhymes & Kids Songs | {config.CHANNEL_NAME}". 1 emoji max. No ALL CAPS.
- DESCRIPTION: first 2 lines (shown before "more") must contain the rhyme name + "nursery rhymes" /
  "kids songs" equivalents and a warm promise to parents (sing-along, learning, bedtime...).
  Then a blank line, the full lyrics, then a short "What your toddler learns" bullet list (2-4),
  then a subscribe call to action for parents naming the channel "{config.CHANNEL_NAME}"
  (e.g. "Subscribe to {config.CHANNEL_NAME} for a new rhyme every week!"). 900-1500 characters total.
- TAGS: 15-25 relevant search phrases, most specific first (rhyme name variants, spellings, the
  language name + "rhymes", "nursery rhymes", "kids songs", "baby songs", "toddler learning",
  the topic), mixed native script + romanised/English. Each tag under 30 characters.
- HASHTAGS: exactly 3 to 5, no spaces, first 3 are shown above the title (most important first).
  Include the brand hashtag #{config.CHANNEL_NAME.replace(' ', '')} as the last one.
Never use other channels' names or brands (e.g. Cocomelon, ChuChu, Pinkfong), never misleading or
unrelated keywords, no tag lists inside the description — these violate YouTube policy.

Return JSON: {{"title": str, "description": str, "tags": [str], "hashtags": [str]}}"""
    data = generate_json(prompt, ledger, f"metadata {lang}")
    tags, used = [], 0
    for t in data.get("tags") or []:
        t = str(t).strip().replace(",", " ")[:30]
        if t and t.lower() not in {x.lower() for x in tags} and used + len(t) + 1 <= TAGS_CHAR_LIMIT:
            tags.append(t)
            used += len(t) + 1
    hashtags = []
    for h in (data.get("hashtags") or [])[:5]:
        h = "#" + str(h).lstrip("#").replace(" ", "")
        if len(h) > 1:
            hashtags.append(h)
    return {
        "title": str(data.get("title") or base_title)[:100],
        "description": str(data.get("description") or "")[:4800],
        "tags": tags,
        "hashtags": hashtags,
    }
