"""Channel logo watermark.

Upload the logo once (any PNG/JPG/WebP, white or transparent background).  We remove the white
background *connected to the image border* only — white inside the artwork (e.g. the outline
around "Kids") is kept — trim it, and add a soft shadow so it reads on any scene.

Every final video then gets the logo at the very bottom (bottom-right by default), small and
slightly transparent so it brands the video without pulling a toddler's eyes off the story.
Shorts/Reels are cut from the branded video, so they carry it too.
"""
from __future__ import annotations

import io
import os
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFilter

from . import config

DIR = config.DATA_DIR / "branding"
DIR.mkdir(parents=True, exist_ok=True)
LOGO = DIR / "logo.png"            # processed, transparent
ORIGINAL = DIR / "logo_original"   # as uploaded

POSITION = os.getenv("LOGO_POSITION", "right")        # right | left | center
WIDTH_PCT = float(os.getenv("LOGO_WIDTH_PCT", "0.17"))  # of video width
OPACITY = float(os.getenv("LOGO_OPACITY", "0.92"))
MARGIN_PCT = 0.012                                      # "extreme bottom": ~1% of height


def has_logo() -> bool:
    return LOGO.exists()


def save_logo(data: bytes) -> Path:
    img = Image.open(io.BytesIO(data)).convert("RGBA")
    ORIGINAL.write_bytes(data)
    img = _remove_border_white(img)
    bbox = img.getchannel("A").point(lambda a: 255 if a > 8 else 0).getbbox()
    if not bbox:
        raise ValueError("The logo looks completely white/empty")
    img = img.crop(bbox)
    if img.width > 1200:  # plenty for a watermark; keeps renders fast
        img = img.resize((1200, round(img.height * 1200 / img.width)), Image.LANCZOS)
    _with_shadow(img).save(LOGO)
    return LOGO


def remove_logo() -> None:
    LOGO.unlink(missing_ok=True)
    ORIGINAL.unlink(missing_ok=True)


def _remove_border_white(img: Image.Image) -> Image.Image:
    rgb = np.asarray(img.convert("RGB")).astype(np.int16)
    alpha = np.asarray(img.getchannel("A")).copy()
    whiteness = rgb.min(axis=2)                       # 255 = pure white
    near_white = (whiteness > 228) & (alpha > 0)
    # flood-fill the near-white region that touches the border (marker value 128)
    # .copy(): Pillow 12 returns a read-only view of the numpy buffer, which floodfill silently ignores
    mask = Image.fromarray(np.where(near_white, 255, 0).astype(np.uint8), "L").copy()
    h, w = alpha.shape
    seeds = [(x, y) for x in range(0, w, max(1, w // 40)) for y in (0, h - 1)] + \
            [(x, y) for y in range(0, h, max(1, h // 40)) for x in (0, w - 1)]
    for sx, sy in seeds:
        if mask.getpixel((sx, sy)) == 255:
            ImageDraw.floodfill(mask, (sx, sy), 128)
    bg = np.asarray(mask) == 128
    bg |= _letter_holes(near_white & ~bg, h)
    # soft edge: background pixels next to the artwork fade by how white they are (anti-aliasing)
    edge = np.asarray(Image.fromarray(bg.astype(np.uint8) * 255).filter(ImageFilter.MinFilter(3))) == 0
    soft = bg & edge
    new_alpha = alpha.astype(np.float32)
    new_alpha[bg] = 0
    new_alpha[soft] = np.clip((255 - whiteness[soft]) * 255 / 27, 0, 255)
    out = img.copy()
    out.putalpha(Image.fromarray(new_alpha.astype(np.uint8), "L"))
    return out


def _letter_holes(enclosed_white: np.ndarray, height: int) -> np.ndarray:
    """White areas fully enclosed by artwork: thick blobs are letter holes (the inside of a, e, d,
    o …) and must become transparent; thin ones are white outlines (around "Kids") and stay.
    A morphological opening keeps only parts at least ~2.5 % of the logo height thick; every
    white region containing such a part is treated as a hole."""
    r = max(2, round(height * 0.025))
    size = 2 * r + 1
    enc = Image.fromarray(enclosed_white.astype(np.uint8) * 255, "L").copy()
    thick = np.asarray(enc.filter(ImageFilter.MinFilter(size if size <= 31 else 31))) > 0
    holes = np.zeros_like(enclosed_white)
    work = enc.copy()
    for y, x in np.argwhere(thick):
        if holes[y, x] or work.getpixel((int(x), int(y))) != 255:
            continue
        ImageDraw.floodfill(work, (int(x), int(y)), 77)
        holes |= np.asarray(work) == 77
    return holes


def _with_shadow(img: Image.Image) -> Image.Image:
    pad = max(6, img.width // 60)
    canvas = Image.new("RGBA", (img.width + pad * 2, img.height + pad * 2), (0, 0, 0, 0))
    shadow = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
    a = img.getchannel("A").point(lambda v: int(v * 0.35))
    shadow.paste((0, 0, 0, 255), (pad + pad // 3, pad + pad // 2), a)
    shadow = shadow.filter(ImageFilter.GaussianBlur(pad / 2))
    canvas.alpha_composite(shadow)
    canvas.alpha_composite(img, (pad, pad))
    return canvas


def logo_height_px(video_w: int) -> int:
    if not has_logo():
        return 0
    with Image.open(LOGO) as im:
        return round(video_w * WIDTH_PCT * im.height / im.width)


def overlay_filter(video_w: int, video_h: int, in_label: str, logo_label: str, out_label: str) -> str:
    """ffmpeg filter snippet placing input `logo_label` on `in_label`."""
    lw = round(video_w * WIDTH_PCT)
    m = round(video_h * MARGIN_PCT)
    x = {"left": f"{m * 2}", "center": "(W-w)/2"}.get(POSITION, f"W-w-{m * 2}")
    return (f"[{logo_label}]scale={lw}:-1,format=rgba,colorchannelmixer=aa={OPACITY}[lg];"
            f"[{in_label}][lg]overlay=x={x}:y=H-h-{m}:format=auto[{out_label}]")


def lyrics_bottom_margin(video_w: int, video_h: int) -> int:
    """Lift the lyrics so they sit just above the logo strip and never overlap it."""
    base = round(video_h * 0.05)
    if not has_logo():
        return base
    return max(base, logo_height_px(video_w) + round(video_h * MARGIN_PCT) + round(video_h * 0.012))


# ------------------------------------------------------------------ intro / outro
BUNDLED = Path(__file__).parent / "assets" / "branding"   # Sunave Kids defaults shipped with the app
CLIP_KINDS = ("intro", "outro")


def clip_path(kind: str) -> Path | None:
    """Uploaded clip, else the bundled default — unless the user removed it."""
    if (DIR / f"{kind}.disabled").exists():
        return None
    up = DIR / f"{kind}.mp4"
    if up.exists():
        return up
    b = BUNDLED / f"{kind}.mp4"
    return b if b.exists() else None


def save_clip(kind: str, data: bytes) -> Path:
    out = DIR / f"{kind}.mp4"
    out.write_bytes(data)
    (DIR / f"{kind}.disabled").unlink(missing_ok=True)
    return out


def remove_clip(kind: str) -> None:
    (DIR / f"{kind}.mp4").unlink(missing_ok=True)
    (DIR / f"{kind}.disabled").write_text("removed")


def restore_default_clip(kind: str) -> None:
    (DIR / f"{kind}.mp4").unlink(missing_ok=True)
    (DIR / f"{kind}.disabled").unlink(missing_ok=True)
