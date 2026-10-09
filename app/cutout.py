"""Engine "Animated cut-outs": backgrounds and characters are separate pictures, code moves them.

* Backgrounds ("plates") are drawn by the image model WITHOUT characters, from a fixed list of locations,
  so the same meadow / pond / bedroom is reused across videos instead of being paid for again.
* Each character gets ONE cut-out sprite (drawn once on a flat colour, keyed out for free) that lives in the
  character library and is reused in every future video, so the character looks identical every time.
* Props that the lyrics mention (an apple, a star, a duck…) are drawn once, cut out and cached by name.
* Frames are drawn in Python: characters hop / sway / dance on the beat of the music, squash and stretch,
  pulse with the voice, cast a contact shadow; props pop up on the word; sparkles twinkle; a gentle camera
  move runs over all of it.  Everything is timed to the audio, per language, frame-exact.
"""
from __future__ import annotations

import hashlib
import math
import os
import random
import re
import shutil
import subprocess
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFilter

from . import beats, characters, chroma, config, toddler
from .costs import CostLedger
from .gemini_client import generate_image

LOCATIONS = {
    "meadow": "a sunny green meadow with soft rolling hills, a few flowers and a bright blue sky with fluffy clouds",
    "pond": "a calm blue pond with lily pads and reeds, a grassy bank and a sunny sky",
    "farm": "a cheerful farmyard with a red barn, a wooden fence and green grass",
    "forest": "a friendly sunlit forest clearing with round trees, soft grass and gentle light rays",
    "garden": "a colourful garden with big flowers, a picket fence and a tidy lawn",
    "beach": "a sunny beach with golden sand, gentle blue waves and a clear sky",
    "bedroom": "a cozy toddler bedroom with a small bed, a soft rug, a toy shelf and warm lamp light",
    "kitchen": "a bright cheerful kitchen with a little table, simple shelves and warm light",
    "bathroom": "a bright bathroom with a bathtub full of bubbles, tiles and a soft mat",
    "playground": "a sunny playground with a slide, a swing and soft green grass",
    "street": "a quiet cartoon street with colourful houses, a pavement and a blue sky",
    "night_sky": "a calm night scene with a big friendly moon, twinkling stars and soft dark-blue hills",
    "rainy_day": "a gentle rainy day with soft blue-grey clouds, tiny raindrops and puddles on green grass",
    "jungle": "a friendly cartoon jungle with big leaves, hanging vines and soft light",
    "classroom": "a bright toddler classroom with a colourful wall, shelves of blocks and a soft rug",
    "park": "a sunny park with a winding path, round trees and a bench",
}
ACTIONS = ("hop", "sway", "dance", "wave", "peek", "grow")
POSITIONS = ("left", "center", "right")
_ACTION_CYCLE = ("hop", "sway", "dance", "hop", "wave", "sway")
_POSITION_CYCLE = ("center", "left", "center", "right")
_LOCATION_CYCLE = ("meadow", "pond", "garden", "forest")
LIGHTING = ("in bright morning light", "in warm golden afternoon light", "in soft pastel evening light")
PLATE_VARIANTS = max(1, int(os.getenv("PLATE_VARIANTS", "2")))   # >1: same place looks a little different per video
INTENSITY = float(os.getenv("CUTOUT_INTENSITY", "1.0"))        # 0.5 = calmer, 1.5 = livelier

SCRIPT_BLOCK = """
THIS VIDEO USES ANIMATED CUT-OUT CHARACTERS over separate backgrounds, so also describe each scene as parts:
- "location": one of {keys}. Keep the same location for several scenes in a row while the story stays in one
  place (use at most 5 different locations in the whole video). Use "custom" only if none fits, and then fill
  "background" with 20-35 words of scenery only (no characters).
- "characters": 1-2 entries {{"name", "position": left|center|right, "action": hop|sway|dance|wave|peek|grow}} - who is
  on screen and what they do (hop = happy bounce, sway = gentle rock, dance = lively, wave = friendly wiggle,
  peek = pops up from below, grow = pops in bigger). Vary positions and actions between scenes.
- "props": 0-2 entries {{"name", "at"}} - simple, concrete things a toddler knows that the lyric mentions (apple, star,
  duck, ball, flower...) which pop up on screen; "at" = 0..1 = how far into the line they appear.
"""
SCRIPT_FIELDS = ' "location": str, "background": str, "characters": [{"name": str, "position": str, "action": str}], "props": [{"name": str, "at": float}],'


# --------------------------------------------------------------------------- script cleanup
def _match_name(name: str, cast_names: list[str]) -> str | None:
    n = (name or "").strip().lower()
    for c in cast_names:
        if c.lower() == n:
            return c
    for c in cast_names:
        if n and (n in c.lower() or c.lower() in n):
            return c
    return None


def normalize_scene(s: dict, cast_names: list[str], index: int) -> None:
    """Make whatever Gemini returned safe to render: valid location/actions/positions, ≤2 chars, ≤2 props."""
    loc = re.sub(r"[^a-z_]+", "_", str(s.get("location", "")).strip().lower()).strip("_")
    bg = str(s.get("background") or "").strip()[:300]
    if loc not in LOCATIONS and loc != "custom":
        loc = "custom" if bg else _LOCATION_CYCLE[index % len(_LOCATION_CYCLE)]
    if loc == "custom" and not bg:
        loc = _LOCATION_CYCLE[index % len(_LOCATION_CYCLE)]
    s["location"], s["background"] = loc, (bg if loc == "custom" else "")

    chars = []
    for c in (s.get("characters") or [])[:2]:
        if not isinstance(c, dict):
            continue
        name = _match_name(c.get("name", ""), cast_names)
        if name and all(name != x["name"] for x in chars):
            chars.append({"name": name, "position": c.get("position") if c.get("position") in POSITIONS else None,
                          "action": c.get("action") if c.get("action") in ACTIONS else None})
    if not chars:
        chars = [{"name": cast_names[index % len(cast_names)], "position": None, "action": None}]
    if len(chars) == 2:
        chars[0]["position"], chars[1]["position"] = "left", "right"
    else:
        chars[0]["position"] = chars[0]["position"] or _POSITION_CYCLE[index % len(_POSITION_CYCLE)]
    for k, c in enumerate(chars):
        c["action"] = c["action"] or _ACTION_CYCLE[(index + k) % len(_ACTION_CYCLE)]
    s["characters"] = chars

    props = []
    for p in (s.get("props") or [])[:2]:
        if not isinstance(p, dict):
            continue
        name = re.sub(r"[^A-Za-z \-]", "", str(p.get("name", ""))).strip().lower()
        words = name.split()
        if not words or len(words) > 3 or name in {n.lower() for n in cast_names}:
            continue
        try:
            at = float(p.get("at", 0.4))
        except (TypeError, ValueError):
            at = 0.4
        props.append({"name": " ".join(words), "at": min(0.9, max(0.1, at))})
    s["props"] = props


def normalize_script(script: dict, chars: list[dict]) -> None:
    names = [c["name"] for c in chars]
    for i, s in enumerate(script["scenes"]):
        normalize_scene(s, names, i)


# ---------------------------------------------------------------------------------- assets
def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:40] or "x"


def sprite_path(cid: str, mode: str) -> Path:
    return config.CHARACTER_DIR / cid / f"sprite_{mode}.png"


def ensure_sprite(cid: str, mode: str, ledger: CostLedger, force: bool = False) -> Path:
    """The character's cut-out. Made once, kept in the library, reused by every video (force = redo it)."""
    dest = sprite_path(cid, mode)
    if dest.exists() and not force:
        ledger.image(f"cut-out {cid}", 1, cached=True)
        return dest
    c = characters.get(cid)
    sheet = characters.sheet_path(cid)
    key = chroma.pick_key([*c["colours"], c.get("signature_item", ""), c["species"]])
    hexcol, kname = chroma.KEYS[key]
    prompt = (
        f"{toddler.style_prompt(mode)}. Character cut-out asset: {characters.describe(c)}. Full body, standing, "
        "facing the viewer, friendly happy expression, the WHOLE body visible with empty margin on every side, "
        f"centred. Background: one flat solid {kname} colour ({hexcol}), completely uniform, no gradient, no floor, "
        "no shadow, no reflection, no text. Keep the exact same character design as the reference sheet. "
        f"Avoid: {toddler.NEGATIVE}."
    )
    img = generate_image(prompt, ledger, f"cut-out {c['name']}", reference_images=[sheet] if sheet else None,
                         aspect_ratio="3:4", cache=not force)
    chroma.cut_out(Image.open(img), key).save(dest)
    return dest


def ensure_prop(name: str, mode: str, ledger: CostLedger) -> Path:
    """A prop (apple, star…) cut out and cached by name, shared by all videos."""
    dest = config.CACHE_DIR / "props" / f"{_slug(name)}_{mode}.png"
    if dest.exists():
        ledger.image(f"prop {name}", 1, cached=True)
        return dest
    dest.parent.mkdir(parents=True, exist_ok=True)
    key = chroma.pick_key(name)
    hexcol, kname = chroma.KEYS[key]
    prompt = (
        f"{toddler.style_prompt(mode)}. Sticker-style prop: a single cute {name}, simple and round, bright colours, "
        f"thick friendly outline, the whole object visible and centred with empty margin. Background: one flat "
        f"solid {kname} colour ({hexcol}), completely uniform, no shadow, no text. Avoid: {toddler.NEGATIVE}."
    )
    img = generate_image(prompt, ledger, f"prop {name}", aspect_ratio="1:1")
    chroma.cut_out(Image.open(img), key).save(dest)
    return dest


def ensure_plate(location: str, custom: str, mode: str, variant: int, ledger: CostLedger) -> Path:
    desc = custom if location == "custom" else LOCATIONS[location]
    light = LIGHTING[variant % len(LIGHTING)]
    prompt = (
        f"{toddler.style_prompt(mode)}. Scene background only, empty of any characters, animals, people or text: "
        f"{desc}, {light}. Wide composition with a clear open ground area across the lower third where characters "
        f"can stand; calm and uncluttered, soft depth, bright toddler-friendly colours. Avoid: {toddler.NEGATIVE}."
    )
    return generate_image(prompt, ledger, f"background {location}", aspect_ratio="16:9")


def prepare(job_dir: Path, script: dict, chars: list[dict], mode: str, job_id: str, ledger: CostLedger,
            step=lambda msg, frac: None) -> dict:
    """Generate / fetch every asset the scenes need, copy them into the job folder (so the job is
    self-contained) and return the scene specs the renderer works from."""
    assets = job_dir / "assets"
    assets.mkdir(exist_ok=True)
    normalize_script(script, chars)
    by_name = {c["name"]: c for c in chars}
    sprites: dict[str, str] = {}
    for i, c in enumerate(chars):
        step(f"Cutting out {c['name']}", 0.10 + 0.05 * i)
        dst = assets / f"sprite_{c['id']}.png"
        shutil.copy(ensure_sprite(c["id"], mode, ledger), dst)
        sprites[c["id"]] = dst.name
    plates: dict[tuple, str] = {}
    props: dict[str, str] = {}
    n = len(script["scenes"])
    specs = []
    for i, s in enumerate(script["scenes"]):
        step(f"Preparing scene {i + 1}/{n}", 0.15 + 0.3 * i / n)
        key = (s["location"], s["background"])
        if key not in plates:
            variant = int(hashlib.md5(f"{job_id}|{s['location']}".encode()).hexdigest()[:4], 16) % PLATE_VARIANTS
            dst = assets / f"bg_{len(plates):02}.png"
            shutil.copy(ensure_plate(s["location"], s["background"], mode, variant, ledger), dst)
            plates[key] = dst.name
        scene_props = []
        for p in s["props"]:
            if p["name"] not in props:
                dst = assets / f"prop_{_slug(p['name'])}.png"
                shutil.copy(ensure_prop(p["name"], mode, ledger), dst)
                props[p["name"]] = dst.name
            scene_props.append({"name": p["name"], "file": props[p["name"]], "at": p["at"]})
        specs.append({
            "bg": plates[key], "camera": s.get("camera", "slow zoom in"), "mood": s.get("mood_colour", "sky blue"),
            "chars": [{"id": by_name[c["name"]]["id"], "name": c["name"], "sprite": sprites[by_name[c["name"]]["id"]],
                       "pos": c["position"], "action": c["action"]} for c in s["characters"]],
            "props": scene_props, "seed": i + 1,
        })
    return {"scenes": specs}


# ---------------------------------------------------------------------------------- renderer
def _clamp01(x: float) -> float:
    return min(1.0, max(0.0, x))


def _ease_out_back(p: float) -> float:
    p = _clamp01(p)
    c1 = 1.70158
    c3 = c1 + 1
    return 1 + c3 * (p - 1) ** 3 + c1 * (p - 1) ** 2


def _smooth(p: float) -> float:
    p = _clamp01(p)
    return p * p * (3 - 2 * p)


def voice_envelope(path: str | None, fps: int, n_frames: int, lead: float) -> np.ndarray | None:
    """Loudness of the spoken line per video frame (0..1), placed where the line starts in the scene."""
    if not path:
        return None
    raw = subprocess.run(["ffmpeg", "-v", "error", "-i", path, "-ac", "1", "-ar", "8000", "-f", "s16le", "-"],
                         capture_output=True).stdout
    x = np.frombuffer(raw, dtype="<i2").astype(np.float32) / 32768.0
    win = 8000 // fps
    k = len(x) // win
    if k < 2:
        return None
    rms = np.sqrt((x[:k * win].reshape(k, win) ** 2).mean(axis=1))
    rms = np.convolve(rms, np.ones(3) / 3, mode="same")
    rms = np.clip(rms / (np.percentile(rms, 95) + 1e-6), 0, 1)
    env = np.zeros(n_frames, np.float32)
    start = int(round(lead * fps))
    seg = rms[: max(0, n_frames - start)]
    env[start:start + len(seg)] = seg
    return env


def _star(size: int, rgb: tuple[int, int, int]) -> Image.Image:
    """Four-point sparkle, soft glow."""
    big = size * 4
    m = Image.new("L", (big, big), 0)
    d = ImageDraw.Draw(m)
    c = big / 2
    r, w = big * 0.48, big * 0.10
    d.polygon([(c, c - r), (c + w, c - w), (c + r, c), (c + w, c + w), (c, c + r), (c - w, c + w), (c - r, c), (c - w, c - w)], fill=255)
    m = m.filter(ImageFilter.GaussianBlur(big * 0.012)).resize((size, size), Image.LANCZOS)
    img = Image.new("RGBA", (size, size), rgb + (255,))
    img.putalpha(m)
    return img


_SPARK_COLOURS = [(255, 217, 61), (255, 248, 231), (255, 163, 215), (107, 203, 255)]


class SceneRenderer:
    """Draws one scene. `frame(t)` is a pure function of time, so scenes render in parallel and re-render
    identically for another language (only the timing inputs change)."""

    def __init__(self, scene: dict, assets: Path, W: int, H: int, fps: int, dur: float, grid: beats.Grid,
                 scene_start: float, lead: float, line_dur: float, env: np.ndarray | None, speaker: int = 0):
        self.W, self.H, self.fps, self.dur = W, H, fps, dur
        self.grid, self.t0, self.lead, self.line_dur, self.env = grid, scene_start, lead, line_dur, env
        self.scene = scene
        bg = Image.open(assets / scene["bg"]).convert("RGB")
        aspect = W / H
        if abs(bg.width / bg.height - aspect) > 0.01:                    # cover-crop to the video's shape
            nw = min(bg.width, round(bg.height * aspect))
            nh = round(nw / aspect)
            l, t = (bg.width - nw) // 2, (bg.height - nh) // 2
            bg = bg.crop((l, t, l + nw, t + nh))
        self.bg, self.k_bg = bg, bg.width / W
        n = len(scene["chars"])
        target_h = H * (0.54 if n == 1 else 0.47)
        self.sprites = []
        for i, c in enumerate(scene["chars"]):
            img = Image.open(assets / c["sprite"]).convert("RGBA")
            th = min(target_h, 0.40 * W * img.height / img.width)
            bh = max(8, round(th * 1.10))
            base = img.resize((max(1, round(img.width * bh / img.height)), bh), Image.LANCZOS)
            x = {"left": 0.27, "center": 0.5, "right": 0.73}[c["pos"]] * W
            self.sprites.append({"base": base, "th": th, "bh": bh, "x": x, "action": c["action"],
                                 "phase": (scene["seed"] * 0.7 + i * 1.9), "talks": i == speaker % n})
        self.ground = 0.905 * H
        self.shadow = self._shadow()
        self.props = []
        xs = [s["x"] for s in self.sprites]
        slots = sorted([(0.78, 0.36), (0.22, 0.36), (0.5, 0.20)], key=lambda p: -min(abs(p[0] * W - x) for x in xs))
        for i, p in enumerate(scene["props"]):
            img = Image.open(assets / p["file"]).convert("RGBA")
            th = min(0.23 * H, 0.26 * W * img.height / img.width)
            bh = max(8, round(th * 1.10))
            self.props.append({"base": img.resize((max(1, round(img.width * bh / img.height)), bh), Image.LANCZOS),
                               "th": th, "bh": bh, "slot": slots[i % len(slots)], "at": p["at"]})
        rng = random.Random(scene["seed"] * 7919)
        self.sparks = []
        for _ in range(14):
            size = max(6, round(H * rng.uniform(0.022, 0.04)))
            self.sparks.append({"x": rng.uniform(0.05, 0.95), "y": rng.uniform(0.05, 0.7), "v": rng.uniform(0.008, 0.026),
                                "f": rng.uniform(0.5, 1.2), "ph": rng.uniform(0, 1), "size": size,
                                "img": _star(size, rng.choice(_SPARK_COLOURS))})

    def _shadow(self) -> Image.Image:
        """Soft ground shadow, about as wide as the widest character."""
        w = max(8, round(max(s["base"].width * s["th"] / s["bh"] for s in self.sprites) * 1.1))
        h = max(4, round(w * 0.16))
        pad = max(3, h // 2)
        m = Image.new("L", (w + 2 * pad, h + 2 * pad), 0)
        ImageDraw.Draw(m).ellipse((pad, pad, pad + w, pad + h), fill=255)
        return m.filter(ImageFilter.GaussianBlur(h * 0.22))

    # ---- camera: every layer is projected through the same zoom/pan, so nothing slides against the background
    def _camera(self, t: float) -> tuple[float, float]:
        p = _smooth(t / max(self.dur, 0.1))
        cam = self.scene["camera"]
        if "zoom out" in cam:
            return 1.06 - 0.06 * p, 0.0
        if "pan left" in cam:
            return 1.06, (0.5 - p) * 0.034 * self.W
        if "pan right" in cam:
            return 1.06, (p - 0.5) * 0.034 * self.W
        return 1.0 + 0.06 * p, 0.0

    def _motion(self, sp: dict, t: float) -> tuple[float, float, float, float, float, float]:
        """(dy px, rotation deg, squash/stretch kx, ky, scale, airborne 0..1) of a character at time t."""
        H, g, a = self.H, self.grid, sp["action"]
        ta = self.t0 + t
        u, n = g.phase(ta)
        air = 4 * u * (1 - u)
        land = math.exp(-(u / 0.10) ** 2) + math.exp(-((1 - u) / 0.10) ** 2)
        A = 0.040 * H * INTENSITY
        dy = th = sq = st = 0.0
        scale = 1.0
        w1 = math.sin(2 * math.pi * (ta - g.offset) / g.period)
        if a in ("hop", "grow", "peek"):
            amp = 1.0 if a == "hop" else 0.6
            dy, sq, st = -A * amp * air, 0.10 * land * amp * INTENSITY, 0.05 * air * amp
        elif a == "dance":
            d = 1 if n % 2 == 0 else -1
            dy, th, sq, st = -0.7 * A * air, d * 5 * math.sin(math.pi * u) * INTENSITY, 0.07 * land, 0.04 * air
        elif a == "sway":
            w2 = math.sin(2 * math.pi * (ta - g.offset) / (2 * g.period))
            th, dy, st = 4.5 * w2 * INTENSITY, -0.012 * H * abs(w2), 0.015 * w1
            air = 0.0
        elif a == "wave":
            th, dy, sq, st = 7 * w1 * INTENSITY, -0.5 * A * air, 0.04 * land, 0.02 * air
        if a == "peek":
            dy += (1 - _ease_out_back(t / 0.9)) * 0.55 * H
        if a == "grow":
            scale = max(0.02, _ease_out_back(t / 0.7))
        ky = (1 - sq + st) * (1 + 0.008 * math.sin(2 * math.pi * t / 2.4 + sp["phase"]))   # idle breathing
        if sp["talks"] and self.env is not None:
            e = float(self.env[min(int(t * self.fps), len(self.env) - 1)])
            ky *= 1 + 0.035 * e
        kx = min(1.2, max(0.85, 1 / ky))
        return dy, th, kx, ky, scale, air

    def frame(self, t: float, still: bool = False) -> Image.Image:
        W, H = self.W, self.H
        z, pan = (1.0, 0.0) if still else self._camera(t)
        cx, cy = W / 2 + pan, H / 2
        hw, hh = W / (2 * z), H / (2 * z)
        k = self.k_bg
        fr = self.bg.resize((W, H), Image.BICUBIC, box=((cx - hw) * k, (cy - hh) * k, (cx + hw) * k, (cy + hh) * k))

        def proj(x: float, y: float) -> tuple[float, float]:
            return W / 2 + (x - W / 2 - pan) * z, H / 2 + (y - H / 2) * z

        # soft sparkles behind the characters
        for s in self.sparks:
            tw = 0.5 + 0.5 * math.sin(2 * math.pi * (0 if still else t * s["f"] + s["ph"]))
            y = (s["y"] - (0 if still else s["v"] * t)) % 0.72
            sx, sy = proj(s["x"] * W, y * H)
            size = max(2, round(s["size"] * (0.6 + 0.5 * tw) * z))
            img = s["img"].resize((size, size), Image.BILINEAR)
            a = img.getchannel("A").point(lambda v, f=0.35 + 0.65 * tw: int(v * f))
            fr.paste(img.convert("RGB"), (round(sx - size / 2), round(sy - size / 2)), a)

        for sp in self.sprites:  # shadows first, so they never cover another character
            dy, th, kx, ky, scale, air = (0, 0, 1, 1, 1, 0) if still else self._motion(sp, t)
            fx, fy = proj(sp["x"], self.ground)
            f = z * (1 - 0.3 * air) * scale
            sw, sh_h = max(2, round(self.shadow.width * f)), max(2, round(self.shadow.height * f))
            sh = self.shadow.resize((sw, sh_h), Image.BILINEAR).point(lambda v, f=0.34 * (1 - 0.5 * air): int(v * f))
            fr.paste((20, 30, 40), (round(fx - sw / 2), round(fy - sh_h / 2)), sh)
        for sp in self.sprites:
            dy, th, kx, ky, scale, air = (0, 0, 1, 1, 1, 0) if still else self._motion(sp, t)
            s0 = sp["th"] / sp["bh"] * z * scale
            nw, nh = max(1, round(sp["base"].width * s0 * kx)), max(1, round(sp["base"].height * s0 * ky))
            img = sp["base"].resize((nw, nh), Image.BILINEAR)
            if abs(th) > 0.05:
                img = img.rotate(th, resample=Image.BICUBIC, expand=True)
            fx, fy = proj(sp["x"], self.ground + dy)
            # rotate about the feet: where the feet end up inside the rotated picture
            rad = math.radians(th)
            pvx = img.width / 2 + (nh / 2) * math.sin(rad)
            pvy = img.height / 2 + (nh / 2) * math.cos(rad)
            fr.paste(img, (round(fx - pvx), round(fy - pvy)), img)

        for pr in self.props:
            tp = self.lead + pr["at"] * self.line_dur
            p = 1.0 if still else (t - tp) / 0.5
            if p <= 0:
                continue
            sc = _ease_out_back(p) if not still else 1.0
            bob = 0.0 if still else 0.012 * H * math.sin(2 * math.pi * (t - tp) / 1.8)
            rot = 0.0 if still else 4 * math.sin(2 * math.pi * (t - tp) / 1.3)
            s0 = pr["th"] / pr["bh"] * z * max(0.02, sc)
            img = pr["base"].resize((max(1, round(pr["base"].width * s0)), max(1, round(pr["base"].height * s0))), Image.BILINEAR)
            if abs(rot) > 0.05:
                img = img.rotate(rot, resample=Image.BICUBIC, expand=True)
            px, py = proj(pr["slot"][0] * W, pr["slot"][1] * H + bob)
            fr.paste(img, (round(px - img.width / 2), round(py - img.height / 2)), img)
        return fr


def render_scene_clip(scene: dict, assets: Path, W: int, H: int, fps: int, n_frames: int, grid: beats.Grid,
                      scene_start: float, lead: float, line_dur: float, voice_path: str | None, speaker: int,
                      out: Path) -> Path:
    """Draw every frame of one scene and pipe it into ffmpeg. Exactly n_frames frames, so timing is frame-exact."""
    env = voice_envelope(voice_path, fps, n_frames, lead)
    r = SceneRenderer(scene, assets, W, H, fps, n_frames / fps, grid, scene_start, lead, line_dur, env, speaker)
    proc = subprocess.Popen(
        ["ffmpeg", "-y", "-loglevel", "error", "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{W}x{H}", "-r", str(fps),
         "-i", "-", "-an", "-c:v", "libx264", "-preset", "veryfast", "-crf", "19", "-pix_fmt", "yuv420p",
         "-r", str(fps), str(out)], stdin=subprocess.PIPE)
    try:
        for f in range(n_frames):
            proc.stdin.write(r.frame(f / fps).tobytes())
    finally:
        proc.stdin.close()
        rc = proc.wait()
    if rc:
        raise RuntimeError(f"ffmpeg failed while drawing a cut-out scene (exit {rc})")
    return out


def still(scene: dict, assets: Path, W: int, H: int, grid: beats.Grid | None = None) -> Image.Image:
    """The scene at rest (all props shown): used as its keyframe and for thumbnails."""
    g = grid or beats.Grid(bpm=toddler.MUSIC["bpm"], offset=0.0)
    return SceneRenderer(scene, assets, W, H, config.FPS, 8.0, g, 0.0, 0.0, 4.0, None).frame(0.0, still=True)
