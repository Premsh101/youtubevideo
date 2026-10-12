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

from . import languages, retry
from .costs import CostLedger
from .gemini_client import generate_json

MAX_REPAIRS = 2

# Unicode ranges → language code, to recognise the language of a pasted poem
_BLOCKS = [("hi", 0x0900, 0x097F), ("bn", 0x0980, 0x09FF), ("pa", 0x0A00, 0x0A7F), ("gu", 0x0A80, 0x0AFF),
           ("ta", 0x0B80, 0x0BFF), ("te", 0x0C00, 0x0C7F), ("kn", 0x0C80, 0x0CFF), ("ml", 0x0D00, 0x0D7F),
           ("ur", 0x0600, 0x06FF), ("ja", 0x3040, 0x30FF)]


# Romanised Hindi ("lakdi ki kathi, kathi pe ghoda"): common words a Hindi theme/poem typed in Latin letters contains
_HINGLISH = {"ki", "ka", "ke", "ko", "se", "mein", "pe", "hai", "hain", "tha", "thi", "aur", "ek", "teen", "char",
             "mera", "meri", "mere", "tera", "teri", "hum", "tum", "yeh", "woh", "nahi", "nahin", "kya", "kaise", "kahan",
             "chal", "chalo", "aao", "gaya", "gayi", "gaye", "wala", "wali", "bhi", "toh", "bada", "chhota", "chota",
             "mummy", "beta", "raja", "rani", "ghoda", "kathi", "lakdi", "chanda", "machli", "pani", "jal", "billi",
             "kutta", "hathi", "bandar", "titli", "phool", "chidiya", "gol", "roti", "dudh", "doodh", "nani", "dadi",
             "chhoti", "nanha", "nanhi", "pyara", "pyari", "sundar", "aasman", "tara", "sooraj", "suraj", "chand",
             "baarish", "barish", "gaana", "geet", "kavita", "bachche", "bachcha", "khel", "khelo", "sona", "neend",
             "lori", "raat", "subah", "udi", "udta", "chala", "chali", "aaya", "aayi", "mama", "dada", "din"}
# other Latin-script languages we support: a few very common words each (a pasted German poem is German, not English)
_LATIN_STOP = {"de": {"und", "ist", "die", "der", "das", "ich", "nicht", "alle", "meine", "ein", "eine", "auf", "mit", "sind", "wir", "du"},
               "fr": {"le", "la", "les", "et", "est", "un", "une", "dans", "je", "vous", "frère", "mon", "ma", "sur", "nous"},
               "es": {"el", "los", "las", "y", "es", "un", "una", "que", "con", "mi", "para", "por", "del", "yo"},
               "it": {"il", "lo", "gli", "e", "è", "un", "una", "che", "con", "mio", "per", "del", "io", "sono"},
               "pt": {"o", "os", "as", "e", "é", "um", "uma", "que", "com", "meu", "para", "do", "da", "eu"},
               "id": {"dan", "yang", "di", "ke", "ini", "itu", "saya", "aku", "ada", "tidak", "dengan", "untuk"}}


def hinglish(text: str) -> bool:
    words = re.findall(r"[a-z]+", (text or "").lower())
    if len(words) < 2:
        return False
    hits = {w for w in words if w in _HINGLISH}
    return len(hits) >= 2 and sum(w in _HINGLISH for w in words) / len(words) >= 0.4


def latin_language(text: str) -> str | None:
    """de/fr/es/... for Latin-script text when its common words say so; None for English or unknown."""
    words = re.findall(r"[a-zà-ÿ]+", (text or "").lower())
    if len(words) < 3:
        return None
    best, score = None, 0.0
    for code, stop in _LATIN_STOP.items():
        hits = [w for w in words if w in stop]
        if len(set(hits)) >= 2 and len(hits) / len(words) > score:
            best, score = code, len(hits) / len(words)
    return best if score >= 0.2 else None


def detect_language(text: str) -> str | None:
    counts: dict[str, int] = {}
    for ch in text:
        o = ord(ch)
        for code, lo, hi in _BLOCKS:
            if lo <= o <= hi:
                counts[code] = counts.get(code, 0) + 1
        if ch.isascii() and ch.isalpha():
            counts["en"] = counts.get("en", 0) + 1
    if not counts:
        return None
    best = max(counts, key=counts.get)
    if best != "en":
        return best
    if hinglish(text):
        return "hi"
    return latin_language(text) or "en"


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
    topic = (params.get("topic") or "").strip()     # a theme typed in Hindi (Devanagari or romanised): Hindi leads
    return (detect_language(topic) if topic else None) or "en", False


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
    """Two Latin-letter ending sounds rhyme when the last vowel group and everything after it match, and
    (for a vowel ending) the consonant before it does too: ara/aara yes, an/aan yes, ina/ara no, ar/ur no."""
    if (a or "").strip() and (a or "").strip() == (b or "").strip():
        return True                                  # identical endings, in any script (Devanagari keys included)
    a, b = norm_sound(a), norm_sound(b)
    if not a or not b:
        return False
    if a == b:
        return True
    def parts(s):
        m = re.search(r"([aeiouy]+)([^aeiouy]*)$", s)
        return (m.group(1), m.group(2), s[:m.start()]) if m else None
    pa, pb = parts(a), parts(b)
    if not pa or not pb or pa[0] != pb[0] or pa[1] != pb[1]:
        return False
    if pa[1]:
        return True
    ca, cb = pa[2][-1:], pb[2][-1:]
    return ca == cb or not ca or not cb


def failing(sounds: list[str]) -> list[tuple[int, int]]:
    return [(a, b) for a, b in couplets(len(sounds)) if not rhymes(sounds[a], sounds[b])]


# ---- Devanagari (Hindi, Marathi, Nepali): a strict check done by code, not by the model's own claims.
_DEV_MATRA = set("\u093e\u093f\u0940\u0941\u0942\u0943\u0944\u0945\u0946\u0947\u0948\u0949\u094a\u094b\u094c\u0962\u0963")
_DEV_VOWEL = set("\u0904\u0905\u0906\u0907\u0908\u0909\u090a\u090b\u090c\u090d\u090e\u090f\u0910\u0911\u0912\u0913\u0914")
_DEV_MOD = set("\u0901\u0902\u0903")          # chandrabindu, anusvara, visarga
_HALANT, _NUKTA = "\u094d", "\u093c"
_LONG = {"\u093f": "\u0940", "\u0941": "\u0942", "\u0907": "\u0908", "\u0909": "\u090a", "\u0945": "\u0947", "\u0949": "\u094b"}


def is_devanagari(text: str) -> bool:
    return any(0x0900 <= ord(c) <= 0x097F for c in text or "")


def _aksharas(word: str) -> list[dict]:
    """Split a Devanagari word into syllable units: {cons: last consonant of the onset, vowel: matra | "a" | None}."""
    out: list[dict] = []
    cur: dict | None = None
    for ch in word:
        if ch == _NUKTA:
            continue
        if ch in _DEV_VOWEL:
            out.append({"cons": "", "vowel": ch, "mod": ""})
            cur = None
        elif ch in _DEV_MATRA:
            if cur is None:
                cur = {"cons": "", "vowel": None, "mod": ""}
                out.append(cur)
            cur["vowel"] = _LONG.get(ch, ch)
        elif ch in _DEV_MOD:
            if out:
                out[-1]["mod"] = "N"
        elif ch == _HALANT:
            if cur is not None:
                cur["vowel"] = "halant"
        elif 0x0915 <= ord(ch) <= 0x0939 or 0x0958 <= ord(ch) <= 0x095F:
            if cur is not None and cur["vowel"] == "halant":   # conjunct: this consonant joins the same unit
                cur["cons"], cur["vowel"] = ch, "a"
            else:
                cur = {"cons": ch, "vowel": "a", "mod": ""}
                out.append(cur)
    return [a for a in out if a["cons"] or a["vowel"]]


_INDEP2MATRA = {"\u0906": "\u093e", "\u0908": "\u0940", "\u0907": "\u0940", "\u090a": "\u0942", "\u0909": "\u0942",
                "\u090f": "\u0947", "\u0910": "\u0948", "\u0913": "\u094b", "\u0914": "\u094c", "\u0911": "\u094b",
                "\u0905": "a", "\u0904": "a"}
# consonants that sound alike at the end of a sung word
_CONS_CLASS = {"\u0936": "\u0938", "\u0937": "\u0938",   # श ष -> स
               "\u0923": "\u0928",                     # ण -> न
               "\u0935": "\u092c",                     # व -> ब
               "\u095c": "\u0921", "\u095d": "\u0922"}  # ड़ ढ़ -> ड ढ


def _ending(word: str) -> dict | None:
    """Structured ending of a Devanagari word: final vowel (or None for a consonant end), the consonant carrying it,
    the vowel before, and nasalisation."""
    ak = _aksharas(re.sub(r"[^\u0900-\u097F]", "", word))
    if not ak:
        return None
    for x in ak:
        x["vowel"] = _INDEP2MATRA.get(x["vowel"], x["vowel"])
        x["cons"] = _CONS_CLASS.get(x["cons"], x["cons"])
    last = ak[-1]
    prev = ak[-2]["vowel"] if len(ak) >= 2 and ak[-2]["vowel"] not in (None, "halant") else "a"
    if last["vowel"] in (None, "a", "halant"):
        return {"final": None, "cons": last["cons"], "prev": prev, "mod": last["mod"]}
    return {"final": last["vowel"], "cons": last["cons"], "prev": prev, "mod": last["mod"]}


def devanagari_key(word: str) -> str:
    """Human-readable rhyming part, for reports: रानी -> नी, आसमान -> ान, घर -> अर."""
    e = _ending(word)
    if not e:
        return ""
    if e["final"] is None:
        return f"{e['prev']}{e['cons']}{e['mod']}"
    return f"{e['cons']}{e['final']}{e['mod']}"


def _match(a: dict, b: dict) -> bool:
    """How Hindi rhymes are heard when sung: a word ending in a consonant rhymes on the vowel before it plus that
    consonant (आसमान / शान, घर / डर); a word ending in a vowel rhymes on that vowel when either the consonant
    carrying it (रानी / पानी, तारा / प्यारा) or the vowel before it (चढ़ी / लगी, गए / रहे) also matches.
    कली / रानी (different consonant AND different vowel before) and चलता / गाती (different final vowel) do not."""
    if a["mod"] != b["mod"]:
        return False
    if a["final"] is None or b["final"] is None:
        return a["final"] is None and b["final"] is None and a["cons"] == b["cons"] and a["prev"] == b["prev"]
    if a["final"] != b["final"]:
        return False
    # same consonant (रानी / पानी); or the same SHORT vowel before (चढ़ी / लगी, गए / रहे) - a long one is not enough
    # (सोना / रोता, पानी / हाथी do not rhyme)
    return a["cons"] == b["cons"] or (a["prev"] == b["prev"] == "a")


_HELPERS = {"है", "हैं", "में", "के", "का", "की", "को", "से", "पर", "पे", "था", "थी", "थे", "ना", "न", "भी", "तो", "हो",
            "रे", "जी", "ही", "और", "या"}


def rhymes_devanagari(a: str, b: str) -> bool | None:
    """True/False for two Devanagari lines, None when the check does not apply (other scripts, empty)."""
    wa = [w for w in re.split(r"[\s,।!?.;:\-\"'()]+", a or "") if is_devanagari(w)]
    wb = [w for w in re.split(r"[\s,।!?.;:\-\"'()]+", b or "") if is_devanagari(w)]
    if not wa or not wb:
        return None
    # helper words at the end (है, हैं, में, के...) are not the rhyme: compare the content words before them
    if wa == wb:
        return True                              # identical lines (a repeated chorus)
    while len(wa) > 1 and len(wb) > 1 and wa[-1] in _HELPERS and wb[-1] in _HELPERS:   # both end in a helper: look before it
        wa, wb = wa[:-1], wb[:-1]
    if wa[-1] == wb[-1] and wa[-1] not in _HELPERS:   # the same content word (... जाता है / ... जाता है)
        return True
    ea, eb = _ending(wa[-1]), _ending(wb[-1])
    return bool(ea and eb) and _match(ea, eb)


# ---- English: spelling -> rough sound of the word's ending, for the simple words toddler rhymes use
_EN_TAIL = [("eigh", "A"), ("igh", "I"), ("ough", "O"), ("augh", "O"), ("tion", "shun"), ("oor", "or"), ("ore", "or"),
            ("our", "or"), ("eer", "Er"), ("ear", "Er"), ("ere", "Er"), ("air", "Er"), ("are", "Er"), ("ee", "E"),
            ("ea", "E"), ("ie", "E"), ("ey", "A"), ("ay", "A"), ("ai", "A"), ("oo", "U"), ("ue", "U"), ("ew", "U"),
            ("oa", "O"), ("oe", "O"), ("ck", "k"), ("ph", "f"), ("ll", "l"), ("ss", "s"), ("tt", "t"), ("nn", "n"),
            ("pp", "p"), ("x", "ks")]
_VOW = set("aeiouAEIOU")
# very common words whose spelling misleads the rules above (sound of the ending, in the same notation)
_EN_WORDS = {"done": "un", "one": "un", "none": "un", "gone": "on", "come": "um", "some": "um", "love": "uv", "dove": "uv",
             "above": "uv", "give": "iv", "live": "iv", "have": "av", "move": "Uv", "lose": "Uz", "whose": "Uz", "two": "U",
             "to": "U", "do": "U", "who": "U", "you": "U", "shoe": "U", "through": "U", "were": "er", "there": "Er",
             "where": "Er", "their": "Er", "bear": "Er", "hair": "Er", "chair": "Er", "wear": "Er", "said": "ed",
             "again": "en", "friend": "end", "great": "At", "eye": "I", "bye": "I", "buy": "I", "my": "I", "by": "I",
             "why": "I", "hi": "I", "go": "O", "so": "O", "no": "O", "toe": "O", "know": "O", "snow": "O", "grow": "O",
             "show": "O", "blow": "O", "slow": "O", "low": "O", "now": "ow", "cow": "ow", "how": "ow", "wow": "ow",
             "me": "E", "be": "E", "we": "E", "he": "E", "she": "E", "the": "E", "sea": "E", "tea": "E", "bee": "E",
             "says": "ez", "eyes": "Iz", "shoes": "Uz", "head": "ed", "bread": "ed", "read": "Ed", "laugh": "af",
             "water": "er", "mother": "er", "father": "er", "brother": "er", "sister": "er", "car": "ar", "star": "ar",
             "are": "ar", "far": "ar", "warm": "orm", "want": "ont", "what": "ot", "put": "Ut", "foot": "Ut", "good": "Ud",
             "could": "Ud", "would": "Ud", "should": "Ud", "book": "Uk", "look": "Uk", "took": "Uk", "cook": "Uk",
             "door": "or", "floor": "or", "four": "or", "more": "or", "pour": "or", "your": "or", "here": "Er",
             "near": "Er", "dear": "Er", "ear": "Er", "year": "Er", "air": "Er", "pear": "Er", "quack": "ak",
             "back": "ak", "duck": "uk", "truck": "uk", "six": "iks", "mix": "iks", "fox": "oks", "box": "oks",
             "socks": "oks", "rocks": "oks", "rain": "An", "train": "An", "plane": "An", "nose": "Oz", "toes": "Oz",
             "goes": "Oz", "grows": "Oz", "clap": "ap", "nap": "ap", "jump": "ump", "up": "up", "cup": "up"}


def english_key(word: str) -> str:
    """Rhyming part of an English word by spelling: star/are -> ar, light/night -> It, sky/high -> I, see/tree -> E."""
    w = re.sub(r"[^a-z]", "", (word or "").lower().replace("'", ""))
    if w in _EN_WORDS:
        return _EN_WORDS[w]
    w = w.replace("qu", "kw")                                     # the u in qu is not a vowel (quack / back)
    if len(w) < 2:
        return ""
    for a, b in _EN_TAIL[:13]:                                    # long tails first (ear/air/ore/are...) before the e-rules
        if w.endswith(a):
            w = w[: -len(a)] + b
            break
    if w[-1] in "aeiou" and sum(c in "aeiou" for c in w) == 1:  # go, so, hi, flu: a lone final vowel is long
        return w[-1].upper()
    if w.endswith("ie") and len(w) <= 3:                           # pie, tie
        w = w[:-2] + "I"
    elif w.endswith("e") and len(w) >= 3 and w[-2] not in _VOW and w[-2] != "r" and w[-3] in "aeiou":
        w = w[:-3] + w[-3].upper() + w[-2]                         # silent e lengthens the vowel: make -> mAk
    elif w.endswith("e") and len(w) >= 3 and w[-2] == "r" and w[-3] in "aeiou":
        w = w[:-1]                                                 # are -> ar, here -> her
    elif w.endswith("y") and len(w) >= 2 and w[-2] not in _VOW:
        if not any(c in "aeiou" for c in w[:-1]):
            return "I"                                                # sky, fly, my
        return w[-2] + "E"                                            # happy -> pE, bunny -> nE (not every -y word rhymes)
    elif w.endswith("ou"):
        w = w[:-2] + "U"                                           # you
    for a, b in _EN_TAIL:
        if a in w[-5:]:
            w = w[: -5] + w[-5:].replace(a, b)
    # from the last vowel group to the end
    i = len(w)
    while i > 0 and w[i - 1] not in _VOW:
        i -= 1
    j = i
    while j > 0 and w[j - 1] in _VOW:
        j -= 1
    key = w[j:]
    return key if len(key) >= 1 and (len(key) >= 2 or key[0].isupper()) else ""


def rhymes_english(a: str, b: str) -> bool | None:
    wa = re.findall(r"[A-Za-z']+", a or "")
    wb = re.findall(r"[A-Za-z']+", b or "")
    if not wa or not wb:
        return None
    if wa[-1].lower() == wb[-1].lower():
        return True
    ka, kb = english_key(wa[-1]), english_key(wb[-1])
    if not ka or not kb:
        return None
    return ka == kb


ENGLISH_RULES = """English rules: PERFECT rhymes only - the ending sounds from the last vowel must be identical (star / are,
light / night, sky / high, play / day, moon / soon). Not slant rhymes (home / alone, love / move), not the same word
twice (except a repeated chorus). Choose the two end words first, then write the lines towards them."""


HINDI_RULES = """Hindi rules (हिंदी बाल-गीत): write the way Hindi children's rhymes are REALLY sung, not translated: short lines
of 12-16 मात्राएँ with a bouncing beat, everyday words a 2-year-old hears at home (मम्मी, पापा, गाय, चिड़िया, रोटी, पानी,
गोल, छोटा, प्यारा), repetition and sounds (छम छम, टप टप, म्याऊँ, भौं भौं). तुकबंदी: the LAST WORD of both lines of a
couplet must end in the same sound - same final vowel-matra together with its consonant (रानी / पानी, तारा / प्यारा,
शान / आसमान, चले / मिले, दूर के / बूर के). Ending both lines with the same helper word (है, में, के, था) is NOT a rhyme by
itself - the words before it must rhyme too (रानी है / पानी है is good, चलता है / गाती है is not). Prefer simple Hindi
over heavy Sanskrit or Urdu words (say आसमान not आकाश, रोशनी not ज्योति) and keep the natural spoken word order.
Models to imitate (traditional): "मछली जल की रानी है / जीवन उसका पानी है", "चंदा मामा दूर के / पुए पकाएँ बूर के",
"हाथी राजा कहाँ चले / सूँड हिलाते कहाँ चले". Give "end_word" in Devanagari."""


def language_rules(lang: str) -> str:
    return HINDI_RULES if lang == "hi" else ENGLISH_RULES if lang == "en" else ""


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
{language_rules(lang)}
- A [CHORUS] line must be word-for-word identical every time it appears.
- Rhythm: each line has about the same number of syllables as its meaning line in English (within 2), 4-9 words,
  simple words a toddler knows, easy to sing, happy tone. Native script only.
For every line also give the rhyming part of the line's ending: "end_word" = the last word, and "end_sound" = the
sounds from the last stressed vowel to the end of that word, spelled in simple Latin letters as pronounced
(e.g. तारा -> "ara", प्यारा -> "ara", आसमान -> "aan", star -> "ar").

Return JSON: {{"title": "short song title in {name}", "lines": [{{"text": str, "end_word": str, "end_sound": str}}]}}"""


def end_word(text: str) -> str:
    """The word that carries the rhyme: punctuation stripped, Hindi helper words (है, में...) skipped."""
    words = [w for w in re.split(r"[\s,।!?.;:\-\"'()]+", text or "") if w]
    while len(words) > 1 and words[-1] in _HELPERS:
        words = words[:-1]
    return words[-1] if words else ""


def _repair_prompt(scenes: list[dict], lang: str, meaning_key: str, texts: list[str], sounds: list[str],
                   fixes: list[tuple[int, int]]) -> tuple[str, list[int]]:
    name = languages.name(lang)
    song = "\n".join(f"{i + 1}. {t}   (ends -{sounds[i]})" for i, t in enumerate(texts))
    todo, rewrite, root = [], [], {}
    for a, b in fixes:
        ca, cb = bool(scenes[a].get("is_chorus")), bool(scenes[b].get("is_chorus"))
        if ca and cb and texts[a] == texts[b]:
            continue                      # the same chorus line twice: nothing to fix
        # never rewrite a chorus line (it repeats elsewhere); otherwise rewrite the second line of the pair
        keep, change = (b, a) if (cb and not ca) else (a, b)
        if keep in rewrite:               # odd count: (3,4) and (4,5) both failing -> aim 5 at line 3's ending too
            keep = root.get(keep, keep)
        root[change] = keep
        rewrite.append(change)
        src = scenes[change].get(meaning_key) or scenes[change]["line_en"]
        w = end_word(texts[keep])
        ending = f'"-{devanagari_key(w)}"' if is_devanagari(w) else f'"-{sounds[keep]}"'
        todo.append(f'- line {change + 1} REWRITE: it must rhyme with line {keep + 1}, which ends with the sound '
                    f'{ending} (word: {w}). Meaning to keep: "{src}"')
    prompt = f"""REPAIR. Song in {name} for toddlers. These couplets do NOT rhyme yet: the two lines of a couplet must END with
words that rhyme (same final sound). Rewrite ONLY the lines marked REWRITE, so that their last word rhymes with the
partner line; keep the meaning, 4-9 simple words, about the same rhythm. Do not change any other line.
{language_rules(lang)}

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
    except retry.QuotaExhausted:
        raise
    except Exception:  # noqa: BLE001
        return None
    return [pairs[k] for k in range(len(pairs)) if verdict.get(k) is False]


def source_poem(params: dict) -> str:
    from . import presets
    if params.get("preset"):
        pr = presets.get(params["preset"])
        if pr and pr.get("poem"):
            return pr["poem"]
    return (params.get("poem") or "").strip()


def _split_poem(poem: str, scenes: list[dict], lang: str, ledger: CostLedger) -> list[str] | None:
    """Lay the owner's own poem (any language) over the scenes, one line each, keeping its words."""
    n = len(scenes)
    if not poem or not n:
        return None
    story = "\n".join(f'{i + 1}. {s.get("line_en", "")}' for i, s in enumerate(scenes))
    prompt = f"""SPLIT. Below is a poem written by the owner in {languages.name(lang)}, and the {n} pictures of the video with the
English meaning of each. Give the poem's text for each picture: exactly {n} lines, in order, keeping the owner's own words
(native script, no translation). Match lines to pictures by meaning; if the poem has fewer lines than pictures, repeat its
chorus or last lines; if more, join short lines.
POEM:
{poem}
PICTURES:
{story}
Return JSON: {{"lines": [{n} strings]}}"""
    try:
        data = generate_json(prompt, ledger, f"poem lines {lang}",
                             validate=lambda d: len(d.get("lines") or []) == n and all(str(x).strip() for x in d["lines"]))
    except retry.QuotaExhausted:
        raise
    except Exception:  # noqa: BLE001
        return None
    return [str(x).strip() for x in data["lines"]]


# ------------------------------------------------------------------------------------ main entry
def ensure_rhyming(script: dict, lang: str, params: dict, ledger: CostLedger) -> dict:
    """Write (or check) the lyrics of `lang` so that couplets rhyme; fills line_<lang> and script['rhyme'][lang]."""
    from .metadata import translate_lines
    scenes = script["scenes"]
    n = len(scenes)
    key = f"line_{lang}"
    src, authoritative = source_language(params)
    report_store = script.setdefault("rhyme", {})
    if authoritative and lang == src and not all(s.get(key) for s in scenes):
        # a poem in a language other than EN/HI: the script only carries EN+HI, so lay the owner's own lines over
        # the scenes first (one cheap call) instead of rewriting their poem from the English translation
        lines = _split_poem(source_poem(params), scenes, lang, ledger)
        if lines:
            for s, text in zip(scenes, lines):
                s[key] = text
    if authoritative and lang == src and all(s.get(key) for s in scenes):
        report_store[lang] = {"ok": True, "skipped": "original poem kept as written", "couplets": len(couplets(n)), "failed": 0}
        return report_store[lang]

    # the language the owner wrote in leads (a Hindi theme: Hindi is the original song, English follows its meaning)
    meaning_key = f"line_{src}" if all(s.get(f"line_{src}") for s in scenes) else "line_en"
    draft = [s.get(key, "") for s in scenes] if all(s.get(key) for s in scenes) else None
    keep_draft = bool(draft) and lang == src
    try:
        ok_shape = lambda d: len(d.get("lines") or []) == n and all(isinstance(x, dict) and x.get("text") for x in d["lines"])
        data = generate_json(_write_prompt(scenes, lang, meaning_key, draft, keep_draft), ledger, f"rhyming lyrics {lang}",
                             validate=ok_shape)
        lines = data["lines"]
    except retry.QuotaExhausted:
        raise                                        # pause the job; never ship a plain translation because of quota
    except Exception as exc:  # noqa: BLE001 - never block a video on this: fall back to plain translation
        for s, text in zip(scenes, translate_lines(script, lang, ledger)):
            s[key] = text
        report_store[lang] = {"ok": False, "couplets": len(couplets(n)), "failed": len(couplets(n)), "tries": 0, "retry": True,
                              "note": f"rhyming step failed ({str(exc)[:120]}); used a plain translation"}
        return report_store[lang]

    texts = [str(x["text"]).strip() for x in lines]
    sounds = [str(x.get("end_sound", "")) for x in lines]
    script.setdefault("titles", {})[lang] = data.get("title") or script.get("titles", {}).get(lang)

    def unify_chorus() -> None:
        """Every repeat of the hook must be word-for-word the first one. A hook may be more than one line
        (a block of consecutive chorus lines): the k-th line of each later block copies the k-th of the first."""
        blocks, cur = [], []
        for i, s in enumerate(scenes):
            if s.get("is_chorus"):
                cur.append(i)
            elif cur:
                blocks.append(cur)
                cur = []
        if cur:
            blocks.append(cur)
        if len(blocks) < 2:
            return
        first = blocks[0]
        for blk in blocks[1:]:
            for k, i in enumerate(blk):
                src = first[min(k, len(first) - 1)]
                texts[i], sounds[i] = texts[src], sounds[src]

    unify_chorus()

    def bad_couplets() -> list[tuple[int, int]]:
        """Couplets whose endings don't match by the writer's own sounds OR that a fresh judge says don't rhyme.
        For Devanagari the strict check by code decides on its own (the model's Latin "sounds" are unreliable)."""
        bad = set(failing(sounds))
        judged = set(_judge(texts, lang, ledger) or [])
        same = {(a, b) for a, b in couplets(n) if texts[a].strip() == texts[b].strip()}   # a repeated chorus line
        judged -= same
        bad -= same
        for a, b in couplets(n):
            strict = rhymes_devanagari(texts[a], texts[b])
            if strict is None and lang == "en":
                strict = rhymes_english(texts[a], texts[b])
            if strict is True and (a, b) not in judged:
                bad.discard((a, b))                      # the writer's Latin "sound" was off, the words do rhyme
                if not rhymes(sounds[a], sounds[b]):
                    sounds[b] = sounds[a]
            elif strict is False:
                bad.add((a, b))                          # a "no" from the code or from the judge always stands
        return sorted(bad | judged)

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
        except retry.QuotaExhausted:
            raise
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
