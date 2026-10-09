"""Two thumbnail options per language video, made from the keyframes that were already generated (₹0).

* Frames are scored (colourful, detailed subject in the middle, not too dark/bright) and the best two that
  are not neighbours are used, so the options really look different.
* Option A "Big title": whole frame, title on top.   Option B "Close-up": zoomed-in on the subject, title at
  the bottom, different colours.  Both get a bright border (stands out in the feed) and the channel logo.
* Text is drawn with libass (same engine as the on-screen lyrics) so Hindi, Tamil, Urdu… are shaped correctly,
  in the same rounded child-friendly fonts.
"""
from __future__ import annotations

import re
import subprocess
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageEnhance

from . import branding, lyrics_overlay

TW, TH = 1280, 720
_EMOJI = re.compile("[\U0001F000-\U0001FAFF☀-➿️‍⬀-⯿]")


def score(img: Image.Image) -> float:
    """Higher = better thumbnail: vivid colours, detail in the middle (the subject), well exposed."""
    a = np.asarray(img.convert("RGB").resize((192, 108))).astype(np.float32)
    r, g, b = a[..., 0], a[..., 1], a[..., 2]
    rg, yb = r - g, 0.5 * (r + g) - b
    colourful = np.hypot(rg.std(), yb.std()) + 0.3 * np.hypot(rg.mean(), yb.mean())
    gray = a.mean(axis=2)
    gy, gx = np.gradient(gray)
    mag = np.hypot(gx, gy)
    centre = mag[27:81, 48:144].mean()
    lum = gray.mean()
    exposure = 0.0 if 70 <= lum <= 210 else min(1.0, (70 - lum) / 70 if lum < 70 else (lum - 210) / 45)
    return float(colourful + 2.0 * centre - 40.0 * exposure)


def pick(frames: list[Path]) -> tuple[int, int]:
    """Indices of the two best frames, not neighbours (so the two options differ)."""
    if len(frames) == 1:
        return 0, 0
    scores = [score(Image.open(f)) for f in frames]
    order = sorted(range(len(frames)), key=lambda i: -scores[i])
    first = order[0]
    gap = max(2, len(frames) // 4)
    second = next((i for i in order[1:] if abs(i - first) >= gap), order[1])
    return first, second


def clean_title(title: str, limit: int = 40) -> str:
    """Short, readable text for the picture: first part of the title, no emoji/branding tail."""
    t = re.split(r"\s[|–—-]\s|\|", title or "")[0]
    t = re.sub(r"\s+", " ", _EMOJI.sub("", t)).strip(" -|:·")
    if len(t) > limit:
        cut = t[:limit].rsplit(" ", 1)[0]
        t = cut if len(cut) >= limit // 2 else t[:limit]
    return t


def _cover(img: Image.Image, zoom: float = 1.0, focus: tuple[float, float] = (0.5, 0.5)) -> Image.Image:
    img = img.convert("RGB")
    scale = max(TW / img.width, TH / img.height) * zoom
    w, h = round(img.width * scale), round(img.height * scale)
    big = img.resize((w, h), Image.LANCZOS)
    left = min(max(0, round(focus[0] * w - TW / 2)), w - TW)
    top = min(max(0, round(focus[1] * h - TH / 2)), h - TH)
    return big.crop((left, top, left + TW, top + TH))


def _gradient(top: bool, strength: float, rgb=(20, 24, 56)) -> Image.Image:
    """Dark-to-clear band so the title is readable on any picture."""
    h = int(TH * 0.46)
    col = np.linspace(strength, 0.0, h) if top else np.linspace(0.0, strength, h)
    alpha = (np.tile(col[:, None], (1, TW)) * 255).astype(np.uint8)
    layer = Image.new("RGBA", (TW, h), rgb + (0,))
    layer.putalpha(Image.fromarray(alpha, "L"))
    return layer


def _ass(text: str, lang: str, align: int, outline_bgr: str, out: Path) -> Path:
    font = lyrics_overlay.FONTS.get(lang, lyrics_overlay.DEFAULT_FONT)
    n = len(text)
    size = 158 if n <= 12 else 138 if n <= 20 else 120 if n <= 30 else 104   # long titles wrap to two lines
    safe = text.replace("\\", "").replace("{", "(").replace("}", ")")
    out.write_text(f"""[Script Info]
ScriptType: v4.00+
PlayResX: {TW}
PlayResY: {TH}
WrapStyle: 0
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: T,{font},{size},&H00FFFFFF,&H00FFFFFF,{outline_bgr},&H80000000,1,0,0,0,100,100,0,0,1,{round(size * 0.13)},{round(size * 0.05)},{align},70,70,{44 if align in (8, 2) else 50},1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
Dialogue: 0,0:00:00.00,0:00:05.00,T,,0,0,0,,{safe}
""", encoding="utf-8")
    return out


def _burn_text(base: Path, ass: Path, out: Path) -> None:
    from . import config
    fonts = Path(config.FONTS_DIR)
    vf = f"ass={ass.name}" + (f":fontsdir={fonts.resolve()}" if fonts.exists() else "")
    subprocess.run(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-i", base.name, "-vf", vf, "-frames:v", "1",
                    "-update", "1", "-q:v", "2", out.name], check=True, cwd=base.parent)


def _frame_border(img: Image.Image, rgb: tuple[int, int, int], width: int = 14) -> None:
    ImageDraw.Draw(img).rectangle((0, 0, TW - 1, TH - 1), outline=rgb, width=width)


def _logo(img: Image.Image, logo: Path | None, corner: str, width_pct: float) -> None:
    if not logo:
        return
    lg = Image.open(logo).convert("RGBA")
    w = round(TW * width_pct)
    lg = lg.resize((w, round(lg.height * w / lg.width)), Image.LANCZOS)
    m = 30
    x = TW - lg.width - m if "right" in corner else m
    y = TH - lg.height - m if "bottom" in corner else m
    img.paste(lg, (x, y), lg)


def build(frames: list[Path], title: str, lang: str, out_dir: Path, logo: Path | None) -> list[dict]:
    """Create thumb_<lang>_A.jpg and thumb_<lang>_B.jpg. Returns [{id, label, file, frame}]."""
    ia, ib = pick(frames)
    text = clean_title(title) or "Nursery Rhymes"
    options = []
    for opt, idx in (("A", ia), ("B", ib)):
        src = Image.open(frames[idx])
        if opt == "A":
            img = _cover(src)
            img = ImageEnhance.Color(img).enhance(1.12)
            img = ImageEnhance.Contrast(img).enhance(1.05)
            rgba = img.convert("RGBA")
            rgba.alpha_composite(_gradient(True, 0.62), (0, 0))
            img = rgba.convert("RGB")
            _frame_border(img, (255, 217, 61))
            _logo(img, logo, "bottom-right", 0.24)
            align, outline, label = 8, "&H00338FFF", "Big title"
        else:
            img = _cover(src, zoom=1.28, focus=(0.5, 0.60))
            img = ImageEnhance.Color(img).enhance(1.18)
            img = ImageEnhance.Contrast(img).enhance(1.06)
            rgba = img.convert("RGBA")
            rgba.alpha_composite(_gradient(False, 0.68, (40, 20, 70)), (0, TH - int(TH * 0.46)))
            img = rgba.convert("RGB")
            _frame_border(img, (255, 163, 215))
            _logo(img, logo, "top-right", 0.22)
            align, outline, label = 2, "&H00D15B3D", "Close-up"
        base = out_dir / f"_thumb_{lang}_{opt}.png"
        img.save(base)
        ass = _ass(text, lang, align, outline, out_dir / f"_thumb_{lang}_{opt}.ass")
        final = out_dir / f"thumb_{lang}_{opt}.jpg"
        _burn_text(base, ass, final)
        base.unlink(missing_ok=True)
        ass.unlink(missing_ok=True)
        options.append({"id": opt, "label": label, "file": final.name, "frame": idx})
    return options
