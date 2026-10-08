"""Reusable character library.

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

SEED_CHARACTERS = [
    {"id": "bunny-pip", "name": "Pip", "species": "bunny", "personality": "gentle and curious",
     "colours": ["candy pink", "cream white"], "signature_item": "a tiny yellow scarf"},
    {"id": "elephant-ellie", "name": "Ellie", "species": "baby elephant", "personality": "kind and giggly",
     "colours": ["sky blue", "soft purple"], "signature_item": "a red balloon tied to her trunk"},
    {"id": "duck-dodo", "name": "Dodo", "species": "duckling", "personality": "silly and brave",
     "colours": ["sunshine yellow", "orange pop"], "signature_item": "little green rain boots"},
    {"id": "cat-mimi", "name": "Mimi", "species": "kitten", "personality": "sleepy and sweet",
     "colours": ["orange pop", "cream white"], "signature_item": "a star-shaped bell collar"},
]


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


def ensure_seeded() -> None:
    if not any(config.CHARACTER_DIR.iterdir()):
        for c in SEED_CHARACTERS:
            save(dict(c))


def design_with_gemini(brief: str, ledger: CostLedger) -> dict:
    """Let Gemini invent a toddler-friendly character from a short brief."""
    prompt = f"""You are a character designer for a preschool (age 1-3) nursery-rhyme YouTube channel.
Design ONE friendly animal character from this brief: "{brief}".
Rules: cute, round, non-scary, big eyes, 2 main bright colours from this palette: {', '.join(k.replace('_',' ') for k in toddler.PALETTE)}.
Return JSON with keys: name (short, easy for toddlers to say), species, personality (3 words),
colours (list of 2), signature_item (one small wearable item), reference_sheet_prompt (one paragraph
describing the character for an image model, front view and side view on plain white background)."""
    data = generate_json(prompt, ledger, "character design")
    data.pop("id", None)
    return save(data)


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
