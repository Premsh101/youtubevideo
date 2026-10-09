"""Rhyming lyrics in every language: written natively, then CHECKED.

Why: translating English lines one by one loses the rhyme (तारा / *कितना सुंदर* …), and a song that does not
rhyme sounds wrong, especially when it is sung.  So each language is written as its own song for the same
pictures, and for every line the model also reports the rhyming sound of the line's ending in Latin letters
("-ara").  We compare those endings couplet by couplet (lines 1+2, 3+4 …).  Couplets that fail are repaired by
rewriting just one line of the pair, up to twice, and the result is stored as a report shown in the app.

The poem you supplied yourself (or a built-in classic) is kept exactly as written in its own language; only
the OTHER languages are written to rhyme.
"""
from __future__ import annotations

import re

from . import languages
from .costs import CostLedger
from .gemini_client import generate_json

MAX_REPAIRS = 2

# Unicode ranges → language code, to recognise the language of a pasted poem
_BLOCKS = [("hi", 0x0900, 0x097F), ("bn", 0x0980, 0x09FF), ("pa", 0x0A00, 0x0A7F), ("gu", 0x0A80, 0x0AFF),
           ("ta", 0x0B80, 0x0BFF), ("te", 0x0C00, 0x0C7F), ("kn", 0x0C80, 0x0CFF), ("ml", 0x0D00, 0x0D7F),
           ("ur", 0x0600, 0x06FF), ("ja", 0x3040, 0x30FF)]


def detect_language(text: str) -> str | None:
    counts: dict[str, int] = {}
    for ch in text:
        o = ord(ch)
        for code, lo, hi in _BLOCKS:
            if lo <= o <= hi:
                counts[code] = counts.get(code, 0) + 1
        if ch.isascii() and ch.isalpha():
            counts["en"] = counts.get("en", 0) + 1
    return max(counts, key=counts.get) if counts else None


def source_language(params: dict) -> tuple[str, bool]:
    """(language the story/meaning comes from, True if that is a poem the user/classic supplied)."""
    from . import presets
    if params.get("preset"):
        pr = presets.get(params["preset"])
        if pr and pr.get("poem"):
            return pr["lang"], True
    poem = (params.get("poem") or "").strip()
    if poem:
        return detect_language(poem) or "en", True
    return "en", False


# ------------------------------------------------------------------------------------ rhyme check
def couplets(n: int) -> list[tuple[int, int]]:
    """Lines that must rhyme with each other: (1,2) (3,4) …; an odd last line joins the previous pair."""
    pairs = [(i, i + 1) for i in range(0, n - 1, 2)]
    if n % 2 == 1 and n >= 3:
        pairs.append((n - 2, n - 1))
    return pairs


def norm_sound(s: str) -> str:
    s = re.sub(r"[^a-z]", "", (s or "").lower())
    for a, b in (("ee", "i"), ("ii", "i"), ("oo", "u"), ("uu", "u"), ("aa", "a"), ("ai", "ai"), ("ay", "ai")):
        s = s.replace(a, b)
    return re.sub(r"([^aeiou])\1+", r"\1", s)


def rhymes(a: str, b: str) -> bool:
    a, b = norm_sound(a), norm_sound(b)
    if not a or not b:
        return False
    if a == b:
        return True
    short, long = sorted((a, b), key=len)
    return len(short) >= 2 and long.endswith(short)


def failing(sounds: list[str]) -> list[tuple[int, int]]:
    return [(a, b) for a, b in couplets(len(sounds)) if not rhymes(sounds[a], sounds[b])]


# ------------------------------------------------------------------------------------ prompts
def _storyboard(scenes: list[dict], key: str) -> str:
    return "\n".join(f'{i + 1}. [{"CHORUS" if s.get("is_chorus") else "verse"}] meaning: "{s.get(key) or s["line_en"]}"'
                     f'   (on screen: {str(s.get("visual", ""))[:90]})' for i, s in enumerate(scenes))


def _write_prompt(scenes: list[dict], lang: str, meaning_key: str, draft: list[str] | None, keep_draft: bool) -> str:
    name = languages.name(lang)
    n = len(scenes)
    pairs = ", ".join(f"({a + 1},{b + 1})" for a, b in couplets(n))
    if draft and keep_draft:
        draft_block = ("Current lyrics (the ORIGINAL song): keep every line that already rhymes and fits; change only "
                       "what is needed so that every couplet rhymes.\n" + "\n".join(f"{i + 1}. {t}" for i, t in enumerate(draft)))
    elif draft:
        draft_block = ("A draft exists but it is only a translation and probably does NOT rhyme. Do not translate it. "
                       "Write a new rhyming song instead; use the draft only as a hint for wording.\n"
                       + "\n".join(f"{i + 1}. {t}" for i, t in enumerate(draft)))
    else:
        draft_block = "There is no draft: write the song from scratch."
    return f"""You are a children's songwriter writing in {name}, for a nursery-rhyme channel for toddlers aged 1-3.
Write a RHYMING sing-along song in {name} for these pictures. Each numbered line is shown on screen with one picture.

STORY (what each line must be about):
{_storyboard(scenes, meaning_key)}

{draft_block}

Write exactly {n} lines, one per picture, in order.
RHYME RULE (most important): the lines in each couplet {pairs} must END with words that rhyme in {name}, meaning
the same final sound, e.g. in English star / are, in Hindi तारा / प्यारा, आसमान / शान, बिल्ली / दिल्ली. Choose end
words first, then build the line. If the picture's meaning can't be said with a rhyming word, change the wording or
the picture's detail slightly. Do NOT translate word for word; write what a native songwriter would sing.
- A [CHORUS] line must be word-for-word identical every time it appears.
- Rhythm: each line has about the same number of syllables as its meaning line in English (within 2), 4-9 words,
  simple words a toddler knows, easy to sing, happy tone. Native script only.
For every line also give the rhyming part of the line's ending: "end_word" = the last word, and "end_sound" = the
sounds from the last stressed vowel to the end of that word, spelled in simple Latin letters as pronounced
(e.g. तारा -> "ara", प्यारा -> "ara", आसमान -> "aan", star -> "ar").

Return JSON: {{"title": "short song title in {name}", "lines": [{{"text": str, "end_word": str, "end_sound": str}}]}}"""


def _repair_prompt(scenes: list[dict], lang: str, meaning_key: str, texts: list[str], sounds: list[str],
                   fixes: list[tuple[int, int]]) -> tuple[str, list[int]]:
    name = languages.name(lang)
    song = "\n".join(f"{i + 1}. {t}   (ends -{sounds[i]})" for i, t in enumerate(texts))
    todo, rewrite = [], []
    for a, b in fixes:
        ca, cb = bool(scenes[a].get("is_chorus")), bool(scenes[b].get("is_chorus"))
        if ca and cb:
            continue                      # two chorus lines are identical by definition
        # never rewrite a chorus line (it repeats elsewhere); otherwise rewrite the second line of the pair
        keep, change = (b, a) if cb else (a, b)
        rewrite.append(change)
        src = scenes[change].get(meaning_key) or scenes[change]["line_en"]
        todo.append(f'- line {change + 1} REWRITE: it must rhyme with line {keep + 1}, which ends with the sound '
                    f'"-{sounds[keep]}" (word: {texts[keep].split()[-1] if texts[keep].split() else ""}). '
                    f'Meaning to keep: "{src}"')
    prompt = f"""REPAIR. Song in {name} for toddlers. These couplets do NOT rhyme yet: the two lines of a couplet must END with
words that rhyme (same final sound). Rewrite ONLY the lines marked REWRITE, so that their last word rhymes with the
partner line; keep the meaning, 4-9 simple words, about the same rhythm. Do not change any other line.

The song now:
{song}

Fix these (a strict check found that each pair really does not rhyme, even if the endings look similar):
{chr(10).join(todo)}

Return JSON: {{"lines": [{{"index": <line number>, "text": str, "end_word": str, "end_sound": str}}]}}"""
    return prompt, rewrite


def _judge(texts: list[str], lang: str, ledger: CostLedger) -> list[tuple[int, int]] | None:
    """Independent second opinion: a fresh request that has not seen the writer's own claims judges every couplet.
    Returns the couplets it says do NOT rhyme (None if the check itself failed — then we rely on the writer)."""
    name = languages.name(lang)
    pairs = couplets(len(texts))
    if not pairs:
        return []
    body = "\n".join(f'Couplet {k + 1}: line {a + 1}: "{texts[a]}"  |  line {b + 1}: "{texts[b]}"' for k, (a, b) in enumerate(pairs))
    prompt = f"""JUDGE. Rhyme check for a toddler song in {name}. For each couplet, say whether the two lines END with
words that really rhyme in {name}: the sounds from the last stressed vowel to the end of the word must be the same
(English star / are, Hindi तारा / प्यारा, आसमान / शान). Sharing only a vowel, only a letter, or a similar look is NOT
enough. Be strict: children will hear it, and a near-miss sounds wrong.

{body}

Return JSON: {{"couplets": [{{"n": 1, "end_a": "last word of the first line, in Latin letters as pronounced",
"end_b": "same for the second line", "rhymes": true or false}}]}}"""
    try:
        res = generate_json(prompt, ledger, f"rhyme check {lang}").get("couplets") or []
        verdict = {int(x["n"]) - 1: bool(x["rhymes"]) for x in res}
    except Exception:  # noqa: BLE001
        return None
    return [pairs[k] for k in range(len(pairs)) if verdict.get(k) is False]


# ------------------------------------------------------------------------------------ main entry
def ensure_rhyming(script: dict, lang: str, params: dict, ledger: CostLedger) -> dict:
    """Write (or check) the lyrics of `lang` so that couplets rhyme; fills line_<lang> and script['rhyme'][lang]."""
    from .metadata import translate_lines
    scenes = script["scenes"]
    n = len(scenes)
    key = f"line_{lang}"
    src, authoritative = source_language(params)
    report_store = script.setdefault("rhyme", {})
    if authoritative and lang == src and all(s.get(key) for s in scenes):
        report_store[lang] = {"ok": True, "skipped": "original poem kept as written", "couplets": len(couplets(n)), "failed": 0}
        return report_store[lang]

    meaning_key = f"line_{src}" if authoritative and all(s.get(f"line_{src}") for s in scenes) else "line_en"
    draft = [s.get(key, "") for s in scenes] if all(s.get(key) for s in scenes) else None
    keep_draft = bool(draft) and lang == (src if authoritative else "en")
    try:
        data = generate_json(_write_prompt(scenes, lang, meaning_key, draft, keep_draft), ledger, f"rhyming lyrics {lang}")
        lines = data.get("lines") or []
        if len(lines) != n or not all(isinstance(x, dict) and x.get("text") for x in lines):
            raise ValueError(f"expected {n} lines, got {len(lines)}")
    except Exception as exc:  # noqa: BLE001 - never block a video on this: fall back to plain translation
        for s, text in zip(scenes, translate_lines(script, lang, ledger)):
            s[key] = text
        report_store[lang] = {"ok": False, "couplets": len(couplets(n)), "failed": len(couplets(n)), "tries": 0,
                              "note": f"rhyming step failed ({str(exc)[:120]}); used a plain translation"}
        return report_store[lang]

    texts = [str(x["text"]).strip() for x in lines]
    sounds = [str(x.get("end_sound", "")) for x in lines]
    script.setdefault("titles", {})[lang] = data.get("title") or script.get("titles", {}).get(lang)

    def unify_chorus() -> None:   # chorus lines must be identical, including their sound
        first = next((i for i, s in enumerate(scenes) if s.get("is_chorus")), None)
        if first is None:
            return
        for i, s in enumerate(scenes):
            if s.get("is_chorus"):
                texts[i], sounds[i] = texts[first], sounds[first]

    unify_chorus()

    def bad_couplets() -> list[tuple[int, int]]:
        """Couplets whose endings don't match by the writer's own sounds OR that a fresh judge says don't rhyme."""
        bad = set(failing(sounds))
        judged = _judge(texts, lang, ledger)
        if judged:
            bad |= set(judged)
        return sorted(bad)

    bad = bad_couplets()
    best = (len(bad), list(texts), list(sounds))
    tries = 0
    while bad and tries < MAX_REPAIRS:
        tries += 1
        prompt, rewrite = _repair_prompt(scenes, lang, meaning_key, texts, sounds, bad)
        if not rewrite:
            break
        try:
            fixed = generate_json(prompt, ledger, f"rhyme repair {lang}").get("lines") or []
        except Exception:  # noqa: BLE001
            break
        for x in fixed:
            try:
                i = int(x["index"]) - 1
            except (KeyError, TypeError, ValueError):
                continue
            if i in rewrite and x.get("text"):
                texts[i], sounds[i] = str(x["text"]).strip(), str(x.get("end_sound", ""))
        unify_chorus()
        bad = bad_couplets()
        if len(bad) < best[0]:
            best = (len(bad), list(texts), list(sounds))
    if len(bad) > best[0]:
        _, texts, sounds = best
        bad = bad_couplets()
    for i, s in enumerate(scenes):
        s[key] = texts[i]
        s[f"end_{lang}"] = sounds[i]
    report_store[lang] = {"ok": not bad, "couplets": len(couplets(n)), "failed": len(bad), "tries": tries,
                          "bad_lines": [[a + 1, b + 1] for a, b in bad]}
    return report_store[lang]
