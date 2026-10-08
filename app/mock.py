"""Offline stand-ins used when MOCK_AI=1 (tests, demos without GCP credentials)."""
from __future__ import annotations

import colorsys
import hashlib
import re
import subprocess
from pathlib import Path

from PIL import Image, ImageDraw

from . import config

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


def mock_json(prompt: str) -> dict:
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
        "scenes": scenes,
    }


def _colour_for(text: str) -> tuple[int, int, int]:
    h = int(hashlib.md5(text.encode()).hexdigest()[:4], 16) / 0xFFFF
    r, g, b = colorsys.hsv_to_rgb(h, 0.55, 1.0)
    return int(r * 255), int(g * 255), int(b * 255)


def mock_image(prompt: str, out: Path, aspect_ratio: str) -> None:
    w, h = (config.VIDEO_W, config.VIDEO_H) if aspect_ratio == "16:9" else (768, 768)
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
        f"[a][b]blend=all_expr='A*(1-T/{dur})+B*(T/{dur})',format=yuv420p",
        "-r", str(config.FPS), "-t", str(dur), "-c:v", "libx264", "-preset", "veryfast", str(out),
    ], check=True)
