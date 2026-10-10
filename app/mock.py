"""Offline stand-ins used when MOCK_AI=1 (tests, demos without GCP credentials)."""
from __future__ import annotations

import colorsys
import hashlib
import re
import subprocess
from pathlib import Path

from PIL import Image, ImageDraw

from . import config, moods

_RHYME_EN = [
    "Twinkle twinkle little star",
    "How I wonder what you are",
    "Up above the world so high",
    "Like a diamond in the sky",
    "When the blazing sun is gone",
    "When he nothing shines upon",
    "Then you show your little light",
    "Twinkle twinkle all the night",
]
_RHYME_HI = [
    "टिमटिम करता नन्हा तारा",
    "कितना सुंदर, कितना प्यारा",
    "ऊँचे ऊँचे आसमान में",
    "हीरे जैसा चमके शान में",
    "जब सूरज ढल जाता है",
    "अँधेरा छा जाता है",
    "तब तुम अपनी ज्योति दिखाते",
    "रात भर यूँ ही टिमटिमाते",
]


def _mock_mood(prompt: str) -> str:
    """What Gemini would answer for "mood": the owner's choice, else a guess from the topic."""
    m = re.search(r"Owner's chosen mood: (\w+)", prompt)
    if m:
        return m.group(1)
    t = re.search(r'typed this theme: "([^"]*)"', prompt)
    return moods.guess(t.group(1)) if t else "playful"


def _mock_known(prompt: str) -> dict:
    """Gemini recognising a rhyme named in the theme: a film song is flagged, a classic is used."""
    t = re.search(r'typed this theme: "([^"]*)"', prompt)
    theme = (t.group(1) if t else "").lower()
    if "lakdi" in theme:
        return {"known_rhyme": "Lakdi Ki Kathi", "copyrighted": True}
    if "machli" in theme:
        return {"known_rhyme": "Machli Jal Ki Rani", "copyrighted": False}
    return {"known_rhyme": None, "copyrighted": False}


def mock_json(prompt: str) -> dict:
    if prompt.startswith("CLIP ANALYSIS."):   # Gemini watching a clip
        dur = float(re.search(r"about (\d+) seconds", prompt).group(1))
        unsafe = "scary" in (re.search(r'file name "([^"]*)"', prompt) or [None, ""])[1].lower()   # only the clip's own file name
        return {"summary": "A friendly character hops across a sunny meadow and waves.", "subjects": ["a friendly character", "a meadow"],
                "actions": ["hops", "waves"], "setting": "a sunny meadow", "mood": "playful", "colours": ["green", "yellow"],
                "moments": [{"t": round(dur * f, 1), "what": w} for f, w in ((0.1, "the character appears"), (0.5, "the character hops"), (0.85, "the character waves"))],
                "has_text_or_logo": False, "kid_safe": not unsafe, "kid_safe_notes": "dark, frightening mood" if unsafe else ""}
    if prompt.startswith("CLIP SCRIPT."):   # lyrics for existing footage
        n = int(re.search(r"composed of (\d+) segments", prompt).group(1))
        return {"title_en": "Hop Hop Hooray", "title_hi": "उछल कूद", "description_en": "A happy hopping rhyme.", "description_hi": "एक मज़ेदार कविता।",
                "mood": _mock_mood(prompt), "tags": ["nursery rhyme", "kids", "hop", "बाल गीत"], "about": "A friendly character hops and waves in a sunny meadow.",
                "scenes": [{"index": i, "line_en": _RHYME_EN[i % len(_RHYME_EN)], "line_hi": _RHYME_HI[i % len(_RHYME_HI)],
                            "is_chorus": i in (0, n - 1) and n >= 6, "visual": f"segment {i + 1}"} for i in range(n)]}
    if prompt.startswith("JUDGE."):   # independent rhyme judge: agrees with everything unless told otherwise
        n = len(re.findall(r"^Couplet \d+:", prompt, flags=re.M))
        return {"couplets": [{"n": k + 1, "end_a": "x", "end_b": "x", "rhymes": True} for k in range(n)]}
    if prompt.startswith("REPAIR."):   # rhyme repair: partner's sound is given in the prompt
        idx = [int(x) for x in re.findall(r"line (\d+) REWRITE", prompt)]
        snd = re.findall(r'sound "-([a-z]+)"', prompt)
        return {"lines": [{"index": i, "text": f"repaired line {i}", "end_word": "w", "end_sound": sn} for i, sn in zip(idx, snd)]}
    if "children's songwriter" in prompt and '"end_sound"' in prompt:   # native rhyming lyrics
        n = int(re.search(r"Write exactly (\d+) lines", prompt).group(1))
        lang = re.search(r"writing in ([A-Za-z]+)", prompt).group(1)
        base = _RHYME_HI if lang == "Hindi" else _RHYME_EN if lang == "English" else None
        lines = []
        for i in range(n):
            snd = "ara" if (i // 2) % 2 == 0 else "ina"
            if i // 2 == 1 and i % 2 == 1:
                snd = "ona"   # 2nd couplet does NOT rhyme on the first attempt: exercises the repair loop
            lines.append({"text": base[i % len(base)] if base else f"{lang} line {i + 1}", "end_word": "w", "end_sound": snd})
        return {"title": f"{lang} rhyme", "lines": lines}
    if '"hashtags"' in prompt:
        return {"title": "Twinkle Twinkle Little Star ⭐ | Nursery Rhymes & Kids Songs",
                "description": "Twinkle Twinkle Little Star nursery rhyme for toddlers. Sing along!\n\nLyrics...",
                "tags": ["twinkle twinkle little star", "nursery rhymes", "kids songs", "baby songs", "toddler learning"],
                "hashtags": ["#nurseryrhymes", "#kidssongs", "#twinkletwinkle"]}
    if '"translated_lines"' in prompt:
        n = int(re.search(r"Below are exactly (\d+) lines", prompt).group(1))
        return {"translated_lines": [f"Funkel funkel kleiner Stern {i + 1}" for i in range(n)], "title": "Funkel Stern"}
    if '"use_existing"' in prompt:
        existing = re.findall(r"id=([a-z0-9-]+):", prompt)
        if existing:
            return {"use_existing": existing[:1], "new_characters": []}
        return {"use_existing": [], "new_characters": [{
            "name": "Tara", "species": "little star", "personality": "bright and giggly",
            "colours": ["sunshine yellow", "soft purple"], "signature_item": "tiny blue mittens",
            "reference_sheet_prompt": "A chubby smiling yellow star with rosy cheeks and tiny blue mittens, front and side view, white background"}]}
    if '"reference_sheet_prompt"' in prompt:
        return {
            "name": "Bunny",
            "species": "bunny",
            "personality": "gentle and curious",
            "colours": ["candy pink", "cream white"],
            "signature_item": "a tiny yellow scarf",
            "reference_sheet_prompt": "A chubby round pink bunny with huge sparkling eyes, tiny yellow scarf, front view and side view, white background",
        }
    scenes = []
    for i, (en, hi) in enumerate(zip(_RHYME_EN, _RHYME_HI)):
        scenes.append({
            "index": i,
            "line_en": en,
            "line_hi": hi,
            "is_chorus": i in (0, 7),
            "location": ["meadow", "meadow", "pond", "night_sky"][(i // 2) % 4],
            "characters": [{"name": "Tara", "position": ["center", "left", "right"][i % 3], "action": ["hop", "sway", "dance", "wave", "peek", "grow"][i % 6]}],
            "props": [{"name": ["star", "apple", "duck", "ball"][i % 4], "at": 0.3 + 0.1 * (i % 3)}] if i % 2 == 0 else [],
            "visual": f"The character looks up at a {['big', 'tiny', 'golden', 'smiling'][i % 4]} star over a soft blue night meadow, scene {i + 1}",
            "camera": ["slow zoom in", "gentle pan right", "slow zoom out", "gentle pan left"][i % 4],
            "mood_colour": ["sky blue", "soft purple", "sunshine yellow", "candy pink"][i % 4],
        })
    return {
        "title_en": "Twinkle Twinkle Little Star",
        "title_hi": "टिमटिम करता नन्हा तारा",
        "description_en": "A gentle rhyme for toddlers.",
        "description_hi": "नन्हे बच्चों के लिए एक प्यारी कविता।",
        "tags": ["nursery rhyme", "toddler", "kids song", "बाल गीत"],
        "mood": _mock_mood(prompt),
        **_mock_known(prompt),
        "scenes": scenes,
    }


def _colour_for(text: str) -> tuple[int, int, int]:
    h = int(hashlib.md5(text.encode()).hexdigest()[:4], 16) / 0xFFFF
    r, g, b = colorsys.hsv_to_rgb(h, 0.55, 1.0)
    return int(r * 255), int(g * 255), int(b * 255)


_fail_budget = {"n": int(__import__("os").getenv("MOCK_FAIL_429", "0"))}


def maybe_fail_429() -> None:
    """Test hook: raise a 429-style error for the first MOCK_FAIL_429 image calls."""
    if _fail_budget["n"] > 0:
        _fail_budget["n"] -= 1
        err = RuntimeError("429 RESOURCE_EXHAUSTED (simulated)")
        err.code = 429
        raise err


def _key_from(prompt: str) -> str | None:
    m = re.search(r"flat solid [a-z ]+ colour \((#[0-9A-Fa-f]{6})\)", prompt)
    return m.group(1) if m else None


def mock_image(prompt: str, out: Path, aspect_ratio: str) -> None:
    maybe_fail_429()
    w, h = (config.VIDEO_W, config.VIDEO_H) if aspect_ratio == "16:9" else (768, 768)
    key = _key_from(prompt)
    if key:   # cut-out asset: a subject on one flat key colour (like the real model is asked to draw)
        prop = "Sticker-style prop" in prompt
        img = Image.new("RGB", (w, h), key)
        d = ImageDraw.Draw(img)
        if prop:
            d.ellipse((w * 0.2, h * 0.2, w * 0.8, h * 0.8), fill=_colour_for(prompt), outline="#333", width=8)
        else:
            cx, cy, rx, ry = w // 2, int(h * 0.56), int(w * 0.3), int(h * 0.36)
            d.ellipse((cx - rx, cy - ry, cx + rx, cy + ry), fill="#FFA3D7", outline="#333", width=8)
            for dx in (-rx // 3, rx // 3):
                d.ellipse((cx + dx - 26, cy - 60, cx + dx + 26, cy - 8), fill="white", outline="#333", width=5)
                d.ellipse((cx + dx - 11, cy - 42, cx + dx + 11, cy - 20), fill="#333")
            d.arc((cx - rx // 3, cy, cx + rx // 3, cy + ry // 2), 20, 160, fill="#333", width=7)
        img.save(out)
        return
    if "Scene background only" in prompt:   # a background plate: sky + ground, no characters
        base = _colour_for(prompt)
        img = Image.new("RGB", (w, h), base)
        d = ImageDraw.Draw(img)
        for y in range(h):
            d.line([(0, y), (w, y)], fill=tuple(min(255, int(c * (0.85 + 0.3 * y / h))) for c in base))
        d.rectangle((0, int(h * 0.74), w, h), fill=(130, 205, 120))
        img.save(out)
        return
    img = Image.new("RGB", (w, h), _colour_for(prompt))
    d = ImageDraw.Draw(img)
    # a friendly blob "character" so motion is visible in the render
    cx, cy, r = w // 2, int(h * 0.6), int(h * 0.22)
    d.ellipse((cx - r, cy - r, cx + r, cy + r), fill="#FFA3D7", outline="#333", width=6)
    for dx in (-r // 3, r // 3):
        d.ellipse((cx + dx - 18, cy - 30, cx + dx + 18, cy + 6), fill="white", outline="#333", width=4)
        d.ellipse((cx + dx - 8, cy - 18, cx + dx + 8, cy - 2), fill="#333")
    d.arc((cx - r // 3, cy + 10, cx + r // 3, cy + r // 2), 0, 180, fill="#333", width=5)
    d.text((24, 24), prompt[:90], fill="#222")
    img.save(out)


def mock_song(scenes, durations, lang, out: Path) -> None:
    """Lullaby bed + hummed lines. Like the real music API it does NOT keep to the plan: each line
    lands progressively later (drift), and the true positions are written next to the file so the
    mock aligner can 'measure' them — this is what the sync test checks the pictures against."""
    from .music import synth_lullaby
    from .tts import _mock_voice
    total = sum(durations) * 1.08
    bed = out.with_suffix(".bed.wav")
    synth_lullaby(total, f"song-{lang}", bed)
    key = f"line_{lang}"
    inputs, delays, t, truth = ["-i", str(bed)], [], 0.0, []
    for i, (s, d) in enumerate(zip(scenes, durations)):
        v = out.with_suffix(f".v{i}.wav")
        _mock_voice(s[key], v)
        inputs += ["-i", str(v)]
        truth.append(round(t + 0.4, 3))
        ms = int((t + 0.4) * 1000)
        delays.append(f"[{i + 1}:a]aformat=sample_rates=44100:channel_layouts=stereo,adelay={ms}|{ms}[d{i}]")
        t += d * 1.08  # drift: the song is slower than planned
    n = len(scenes)
    fc = ";".join(delays + ["[0:a]" + "".join(f"[d{i}]" for i in range(n)) + f"amix=inputs={n + 1}:normalize=0[a]"])
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", *inputs, "-filter_complex", fc, "-map", "[a]",
                    "-t", f"{total:.2f}", "-c:a", "libmp3lame", "-q:a", "4", str(out)], check=True)
    for f in out.parent.glob(out.stem + ".*.wav"):
        f.unlink()
    out.with_suffix(".truth.json").write_text(__import__("json").dumps(truth))


def mock_clip(first: Path, last: Path | None, out: Path) -> None:
    dur = config.VEO_CLIP_SECONDS
    if last is None:
        last = first
    subprocess.run([
        "ffmpeg", "-y", "-loglevel", "error",
        "-loop", "1", "-t", str(dur), "-i", str(first),
        "-loop", "1", "-t", str(dur), "-i", str(last),
        "-filter_complex",
        f"[0:v]scale={config.VIDEO_W}:{config.VIDEO_H},setsar=1[a];"
        f"[1:v]scale={config.VIDEO_W}:{config.VIDEO_H},setsar=1[b];"
        # like a real Veo shot: things move all the time (gentle drift + zoom), fading to the end keyframe
        f"[a]scale=iw*1.25:-2,crop={config.VIDEO_W}:{config.VIDEO_H}:x='(in_w-out_w)*t/{dur}':y='(in_h-out_h)/2'[am];"
        f"[am][b]blend=all_expr='A*(1-T/{dur})+B*(T/{dur})',format=yuv420p",
        "-r", str(config.FPS), "-t", str(dur), "-c:v", "libx264", "-preset", "veryfast", str(out),
    ], check=True)
