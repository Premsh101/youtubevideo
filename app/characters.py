"""Reusable character library.

Gemini casts each video: it reuses characters already in the library when they
fit the poem and invents new ones only when needed; new ones are saved here.
A character = JSON profile + a generated reference sheet (front/side view).  The
sheet is paid for once and then passed as a reference image to every scene
render, which is what keeps the character identical across videos and scenes.
"""
from __future__ import annotations

import json
import re
import shutil
import time
from pathlib import Path

from . import config, toddler
from .costs import CostLedger
from .gemini_client import generate_image, generate_json


def _slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-") or f"char-{int(time.time())}"


def _path(cid: str) -> Path:
    return config.CHARACTER_DIR / cid


def list_characters() -> list[dict]:
    out = []
    for d in sorted(config.CHARACTER_DIR.iterdir()):
        f = d / "character.json"
        if f.exists():
            c = json.loads(f.read_text())
            c["has_sheet"] = (d / "sheet.png").exists()
            out.append(c)
    return out


def get(cid: str) -> dict:
    f = _path(cid) / "character.json"
    if not f.exists():
        raise KeyError(f"unknown character {cid}")
    return json.loads(f.read_text())


def sheet_path(cid: str) -> Path | None:
    p = _path(cid) / "sheet.png"
    return p if p.exists() else None


def describe(c: dict) -> str:
    """One-line description injected into every scene prompt (text anchor for consistency)."""
    return (f"{c['name']} the {c['species']}: {c['personality']}, "
            f"{' and '.join(c['colours'])} coloured, always wearing {c['signature_item']}")


def save(c: dict) -> dict:
    cid = c.get("id") or _slug(c["name"])
    c["id"] = cid
    d = _path(cid)
    d.mkdir(parents=True, exist_ok=True)
    (d / "character.json").write_text(json.dumps(c, ensure_ascii=False, indent=1))
    return c


def delete(cid: str) -> None:
    shutil.rmtree(_path(cid), ignore_errors=True)


def ensure_sheet(cid: str, mode: str, ledger: CostLedger) -> Path:
    """Generate (once) the reference sheet used for every later render."""
    existing = sheet_path(cid)
    if existing:
        ledger.image(f"sheet {cid}", 1, cached=True)
        return existing
    c = get(cid)
    prompt = (
        f"Character reference sheet, {toddler.style_prompt(mode)}. "
        f"{c.get('reference_sheet_prompt') or describe(c)}. "
        "Show the SAME character twice: front view on the left, side view on the right, "
        "full body, neutral happy pose, plain white background, no text. "
        f"Avoid: {toddler.NEGATIVE}."
    )
    img = generate_image(prompt, ledger, f"sheet {cid}", aspect_ratio="16:9")
    dest = _path(cid) / "sheet.png"
    shutil.copy(img, dest)
    c["mode"] = mode
    save(c)
    return dest


def design_from_description(description: str, mode: str, ledger: CostLedger, max_chars: int = 2) -> list[dict]:
    """User described the character(s) in their own words; Gemini turns that into consistent,
    toddler-friendly character profiles (saved to the library for reuse) and draws the sheets."""
    prompt = f"""You are the character designer for a preschool (age 1-3) nursery-rhyme YouTube channel.
The user described the character(s) they want:
<<{description}>>

Turn this into at most {max_chars} character(s). Keep everything the user specified (species, names,
colours, clothing); fill in anything missing so they are cute, round, non-scary, with big friendly eyes
and bright colours from: {', '.join(k.replace('_', ' ') for k in toddler.PALETTE)}.

Return JSON:
{{"characters": [{{"name": short name toddlers can say, "species": str, "personality": "3 words",
   "colours": [2 colours], "signature_item": "one small wearable item",
   "reference_sheet_prompt": "one paragraph describing the character for an image model"}}]}}"""
    data = generate_json(prompt, ledger, "character design")
    items = data.get("characters") if isinstance(data.get("characters"), list) else [data]
    cast = []
    for c in items[:max_chars]:
        if not c.get("name") or not c.get("species"):
            continue
        c.pop("id", None)
        c.setdefault("personality", "happy and friendly")
        c.setdefault("colours", ["sunshine yellow", "sky blue"])
        c.setdefault("signature_item", "a little red bow")
        c["described_by_user"] = description
        cast.append(save(c))
    if not cast:
        raise RuntimeError("Gemini could not design a character from that description — try adding the animal/object type")
    for c in cast:
        ensure_sheet(c["id"], mode, ledger)
    return cast


def cast_with_gemini(topic: str | None, poem: str | None, mode: str, ledger: CostLedger,
                     max_chars: int = 2) -> list[dict]:
    """Ask Gemini which characters the rhyme needs: reuse from the library if they fit,
    otherwise design new ones (saved for future videos)."""
    library = list_characters()
    lib_text = "\n".join(f"- id={c['id']}: {describe(c)}" for c in library) or "(library is empty)"
    subject = f'the user\'s poem:\n"""\n{poem}\n"""' if poem else f'the topic "{topic or "a happy day with friends"}"'
    prompt = f"""You are the casting director and character designer for a preschool (age 1-3)
nursery-rhyme YouTube channel. The next video is about {subject}.

Existing reusable characters:
{lib_text}

Pick at most {max_chars} characters in total. PREFER reusing existing characters when they fit the
rhyme (reuse saves money and toddlers love familiar faces). Only invent a new character when the
rhyme clearly needs one (e.g. the poem is about a cow and we have no cow).
New characters: cute, round, non-scary animals or friendly objects, big eyes, 2 main bright colours
from: {', '.join(k.replace('_', ' ') for k in toddler.PALETTE)}.

Return JSON:
{{"use_existing": [ids from the library],
  "new_characters": [{{"name": short name toddlers can say, "species": str, "personality": "3 words",
    "colours": [2 colours], "signature_item": "one small wearable item",
    "reference_sheet_prompt": "one paragraph describing the character for an image model"}}]}}"""
    data = generate_json(prompt, ledger, "casting")
    known = {c["id"] for c in library}
    cast = [get(cid) for cid in data.get("use_existing", []) if cid in known]
    for nc in data.get("new_characters", []):
        if len(cast) >= max_chars:
            break
        nc.pop("id", None)
        cast.append(save(nc))
    if not cast:  # safety net: Gemini returned nothing usable
        cast = library[:1] or [save({"name": "Pip", "species": "bunny", "personality": "gentle and curious",
                                     "colours": ["candy pink", "cream white"], "signature_item": "a tiny yellow scarf"})]
    for c in cast:
        ensure_sheet(c["id"], mode, ledger)
    return cast
