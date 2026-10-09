"""Cut a character / prop out of its flat-colour background (free, no AI, no model download).

The image model is asked to draw the subject on one flat key colour (green, or magenta/blue when the
subject itself is green/pink/blue).  We key that colour out with a smooth threshold, remove the colour
spill on the edge pixels, drop specks and trim to the subject.  If the model ignored the instruction
(the border isn't the key colour) we fall back to removing whatever uniform colour touches the border.
"""
from __future__ import annotations

import numpy as np
from PIL import Image, ImageDraw, ImageFilter

KEYS = {
    "green": ("#00FF00", "bright green"),
    "magenta": ("#FF00FF", "bright magenta"),
    "blue": ("#0000FF", "pure blue"),
}


def pick_key(colour_names: list[str] | str) -> str:
    """Key colour that does not occur in the subject: green unless the subject has green in it, etc."""
    text = " ".join(colour_names) if isinstance(colour_names, list) else str(colour_names)
    text = text.lower()
    has = {"green": any(w in text for w in ("green", "leaf", "frog", "grass", "broccoli", "cucumber", "pea", "turtle", "tree")),
           "magenta": any(w in text for w in ("pink", "purple", "magenta", "violet", "red", "berry", "grape")),
           "blue": any(w in text for w in ("blue", "sky", "water", "ocean", "whale", "fish"))}
    for name in ("green", "magenta", "blue"):
        if not has[name]:
            return name
    return "green"


def _smooth(x: np.ndarray, lo: float, hi: float) -> np.ndarray:
    t = np.clip((x - lo) / (hi - lo), 0, 1)
    return t * t * (3 - 2 * t)


def _keyness(rgb: np.ndarray, key: str) -> np.ndarray:
    """0 = definitely subject, 1 = definitely key-coloured background (works for dark/light key shades)."""
    r, g, b = rgb[..., 0], rgb[..., 1], rgb[..., 2]
    if key == "green":
        return _smooth(g - np.maximum(r, b), 35, 105)
    if key == "magenta":
        return _smooth(np.minimum(r, b) - g, 60, 130)
    return _smooth(b - np.maximum(r, g), 55, 125)


def _despill(rgb: np.ndarray, key: str, mask: np.ndarray) -> np.ndarray:
    out = rgb.copy()
    r, g, b = out[..., 0], out[..., 1], out[..., 2]
    if key == "green":
        g2 = np.minimum(g, np.maximum(r, b))
        out[..., 1] = np.where(mask, g2, g)
    elif key == "magenta":
        lim = g + (np.maximum(r, b) - g) * 0.35
        out[..., 0] = np.where(mask, np.minimum(r, lim), r)
        out[..., 2] = np.where(mask, np.minimum(b, lim), b)
    else:
        b2 = np.minimum(b, np.maximum(r, g))
        out[..., 2] = np.where(mask, b2, b)
    return out


def _border_pixels(arr: np.ndarray, width: int = 4) -> np.ndarray:
    return np.concatenate([arr[:width].reshape(-1, arr.shape[-1]), arr[-width:].reshape(-1, arr.shape[-1]),
                           arr[:, :width].reshape(-1, arr.shape[-1]), arr[:, -width:].reshape(-1, arr.shape[-1])])


def _alpha_by_chroma(rgb: np.ndarray, key: str) -> np.ndarray | None:
    k = _keyness(rgb, key)
    border = _border_pixels(k[..., None])[:, 0]
    if (border > 0.6).mean() < 0.7:   # the model did not draw a (mostly) key-coloured border
        return None
    return 1.0 - k


def _alpha_by_border_flood(img: Image.Image, tol: float = 42.0) -> np.ndarray:
    """Fallback: remove the uniform colour touching the border (any colour), keep enclosed regions."""
    rgb = np.asarray(img.convert("RGB")).astype(np.float32)
    bg = np.median(_border_pixels(rgb), axis=0)
    dist = np.sqrt(((rgb - bg) ** 2).sum(axis=2))
    near = Image.fromarray(np.where(dist < tol, 255, 0).astype(np.uint8), "L").copy()  # .copy(): Pillow 12 arrays are read-only
    h, w = dist.shape
    seeds = [(x, y) for x in range(0, w, max(1, w // 50)) for y in (0, h - 1)] + \
            [(x, y) for y in range(0, h, max(1, h // 50)) for x in (0, w - 1)]
    for sx, sy in seeds:
        if near.getpixel((sx, sy)) == 255:
            ImageDraw.floodfill(near, (sx, sy), 128)
    bgmask = np.asarray(near) == 128
    soft = np.clip((dist - tol * 0.6) / (tol * 0.8), 0, 1)
    return np.where(bgmask, np.minimum(soft, 0.0 + (dist >= tol)), 1.0).astype(np.float32)


def cut_out(img: Image.Image, key: str = "green", pad: int = 6) -> Image.Image:
    """RGBA subject, trimmed to its bounding box (+pad), edges clean and slightly soft."""
    img = img.convert("RGB")
    rgb = np.asarray(img).astype(np.float32)
    alpha = _alpha_by_chroma(rgb, key)
    if alpha is None:
        alpha = _alpha_by_border_flood(img)
        spill_mask = np.zeros(alpha.shape, bool)
    else:
        spill_mask = alpha < 0.98
        spill_mask = np.asarray(Image.fromarray(spill_mask.astype(np.uint8) * 255).filter(ImageFilter.MaxFilter(5))) > 0
        rgb = _despill(rgb, key, spill_mask)
    a = Image.fromarray(np.clip(alpha * 255, 0, 255).astype(np.uint8), "L")
    a = a.filter(ImageFilter.MinFilter(3)).filter(ImageFilter.MedianFilter(5)).filter(ImageFilter.GaussianBlur(0.8))
    out = Image.fromarray(np.clip(rgb, 0, 255).astype(np.uint8), "RGB").convert("RGBA")
    out.putalpha(a)
    box = a.point(lambda v: 255 if v > 24 else 0).getbbox()
    if not box:
        raise ValueError("Nothing left after removing the background — the subject may be the same colour as it")
    l, t, r, b = box
    out = out.crop((max(0, l - pad), max(0, t - pad), min(out.width, r + pad), min(out.height, b + pad)))
    return out
