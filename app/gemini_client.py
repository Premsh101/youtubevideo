"""Thin wrapper around google-genai pointed at Vertex AI (service-account JSON auth).

All generation (text JSON, images with reference images, Veo clips) goes through
here so cost accounting and the MOCK_AI fallback live in one place.
"""
from __future__ import annotations

import hashlib
import io
import json
import re
import time
from pathlib import Path

from PIL import Image, ImageDraw

from . import config, retry
from .costs import CostLedger

_client = None


def client():
    global _client
    if _client is None:
        from google import genai
        if not config.GCP_PROJECT:
            raise RuntimeError(
                "No GCP project: set GOOGLE_APPLICATION_CREDENTIALS to your Vertex service-account "
                "JSON (project_id is read from it) or set GOOGLE_CLOUD_PROJECT."
            )
        _client = genai.Client(vertexai=True, project=config.GCP_PROJECT, location=config.GCP_LOCATION)
    return _client


def _hash(*parts: str | bytes) -> str:
    h = hashlib.sha256()
    for p in parts:
        h.update(p if isinstance(p, bytes) else p.encode())
    return h.hexdigest()[:24]


# --------------------------------------------------------------------------- text
def generate_json(prompt: str, ledger: CostLedger, detail: str, cache: bool = True) -> dict:
    """Ask Gemini for strict JSON. Cached by prompt hash so re-runs are free."""
    key = _hash("json", config.TEXT_MODEL, prompt)
    cache_file = config.CACHE_DIR / "text" / f"{key}.json"
    if cache and cache_file.exists():
        ledger.text(detail, 0, 0, cached=True)
        return json.loads(cache_file.read_text())

    if config.MOCK_AI:
        from .mock import mock_json
        data = mock_json(prompt)
        ledger.text(detail, 1500, 1500)
    else:
        from google.genai import types
        resp = retry.guarded("text", lambda: client().models.generate_content(
            model=config.TEXT_MODEL,
            contents=prompt,
            config=types.GenerateContentConfig(
                response_mime_type="application/json",
                temperature=0.9,
            ),
        ))
        usage = resp.usage_metadata
        ledger.text(detail, usage.prompt_token_count or 0, usage.candidates_token_count or 0)
        data = _parse_json(resp.text)
    cache_file.parent.mkdir(parents=True, exist_ok=True)
    cache_file.write_text(json.dumps(data, ensure_ascii=False, indent=1))
    return data


def _parse_json(text: str) -> dict:
    text = text.strip()
    text = re.sub(r"^```(?:json)?", "", text).strip()
    text = re.sub(r"```$", "", text).strip()
    return json.loads(text)


# -------------------------------------------------------------------------- images
def generate_image(prompt: str, ledger: CostLedger, detail: str,
                   reference_images: list[Path] | None = None,
                   aspect_ratio: str = "16:9", cache: bool = True) -> Path:
    """Generate one PNG. Reference images (character sheets) are passed in-context so
    the same character is drawn every time.  Cached by prompt + reference hashes."""
    refs = reference_images or []
    key = _hash("img", config.IMAGE_MODEL, prompt, aspect_ratio, *[p.read_bytes() for p in refs])
    out = config.CACHE_DIR / "images" / f"{key}.png"
    if cache and out.exists():
        ledger.image(detail, 1, cached=True)
        return out
    out.parent.mkdir(parents=True, exist_ok=True)

    if config.MOCK_AI:
        from .mock import mock_image
        retry.guarded("image", lambda: mock_image(prompt, out, aspect_ratio))
        ledger.image(detail, 1)
        return out

    from google.genai import types
    contents: list = []
    if refs:
        contents.append(
            "Use these reference images as the exact character designs (same face, colours, "
            "proportions and outfit). Draw a NEW scene described below featuring them:"
        )
        for p in refs:
            contents.append(Image.open(p))
    contents.append(prompt)

    for attempt in range(3):  # retries here are for "model returned no image"; quota retries live in retry.py
        resp = retry.guarded("image", lambda: client().models.generate_content(
            model=config.IMAGE_MODEL,
            contents=contents,
            config=types.GenerateContentConfig(
                response_modalities=["IMAGE"],
                image_config=types.ImageConfig(aspect_ratio=aspect_ratio),
            ),
        ))
        for part in resp.candidates[0].content.parts:
            if part.inline_data and part.inline_data.data:
                img = Image.open(io.BytesIO(part.inline_data.data)).convert("RGB")
                img.save(out)
                ledger.image(detail, 1)
                return out
        time.sleep(1.5 * (attempt + 1))
    raise RuntimeError(f"Image model returned no image for: {detail}")


# ----------------------------------------------------------------------------- veo
def generate_video_clip(prompt: str, first_frame: Path, last_frame: Path | None,
                        ledger: CostLedger, detail: str, cache: bool = True) -> Path:
    """8-second Veo clip anchored on a first (and optionally last) keyframe.
    Chaining last_frame(N) == first_frame(N+1) makes consecutive scenes flow."""
    key = _hash("veo", config.VEO_MODEL, prompt, first_frame.read_bytes(),
                last_frame.read_bytes() if last_frame else b"")
    out = config.CACHE_DIR / "veo" / f"{key}.mp4"
    if cache and out.exists():
        ledger.veo(detail, config.VEO_CLIP_SECONDS, cached=True)
        return out
    out.parent.mkdir(parents=True, exist_ok=True)

    if config.MOCK_AI:
        from .mock import mock_clip
        mock_clip(first_frame, last_frame, out)
        ledger.veo(detail, config.VEO_CLIP_SECONDS)
        return out

    from google.genai import types
    cfg = dict(
        aspect_ratio="16:9",
        duration_seconds=config.VEO_CLIP_SECONDS,
        number_of_videos=1,
        generate_audio=False,  # we add voice + music ourselves (cheaper, bilingual)
        negative_prompt="text, letters, watermark, scary, fast cuts, flicker",
    )
    if last_frame:
        cfg["last_frame"] = types.Image(image_bytes=last_frame.read_bytes(), mime_type="image/png")
    op = retry.guarded("veo", lambda: client().models.generate_videos(
        model=config.VEO_MODEL,
        prompt=prompt,
        image=types.Image(image_bytes=first_frame.read_bytes(), mime_type="image/png"),
        config=types.GenerateVideosConfig(**cfg),
    ))
    while not op.done:
        time.sleep(8)
        op = client().operations.get(op)
    if op.error:
        raise RuntimeError(f"Veo failed: {op.error}")
    vid = op.response.generated_videos[0].video
    data = vid.video_bytes
    if data is None and getattr(vid, "uri", None):
        data = client().files.download(file=vid)
    if not data:
        raise RuntimeError("Veo returned no video bytes (set no output_gcs_uri so bytes are inlined)")
    out.write_bytes(data)
    ledger.veo(detail, config.VEO_CLIP_SECONDS)
    return out


def placeholder_image(path: Path, text: str, colour: str = "#FFD93D") -> Path:
    img = Image.new("RGB", (config.VIDEO_W, config.VIDEO_H), colour)
    ImageDraw.Draw(img).text((40, 40), text, fill="#333333")
    img.save(path)
    return path
