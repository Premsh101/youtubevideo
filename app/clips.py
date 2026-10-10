"""Your own short videos ("clips"): upload once, reuse in any number of videos.

* `add()` stores an upload in the clip library (data/clips/<id>/), measures it (length, shape, sound), and makes a
  preview picture.
* `analyze()` lets Gemini WATCH the clip (a small, silent, low-frame-rate copy is sent, so it is cheap and well
  under the request size limit) and describe what happens and when; cached per clip, so reusing a clip is free.
* `plan_segments()` cuts every clip into pieces of about one lyric line each (~7 s), so the lyrics follow the
  footage; `cut_segment()` produces a frame-exact piece already fitted to the video's size (vertical or square clips
  get a blurred background instead of being cropped).
"""
from __future__ import annotations

import json
import shutil
import subprocess
import time
import uuid
from pathlib import Path

from . import config
from .costs import CostLedger
from .gemini_client import generate_json

DIR = config.DATA_DIR / "clips"
DIR.mkdir(parents=True, exist_ok=True)
ALLOWED = {".mp4", ".mov", ".m4v", ".webm", ".mkv", ".avi"}
MAX_BYTES = 300 * 1024 * 1024
MAX_SECONDS = 120.0
MIN_SECONDS = 1.0
SECONDS_PER_LINE = 7.0       # one lyric line per ~7 s of footage, like the other engines


def _dir(cid: str) -> Path:
    if not cid or "/" in cid or ".." in cid:
        raise KeyError(cid)
    return DIR / cid


def probe(path: Path) -> dict:
    """Length, displayed size (phone rotation applied), frame rate and whether it has sound."""
    out = subprocess.run(["ffprobe", "-v", "error", "-show_entries",
                          "format=duration:stream=codec_type,width,height,r_frame_rate:stream_tags=rotate:stream_side_data=rotation",
                          "-of", "json", str(path)], capture_output=True, text=True)
    if out.returncode:
        raise ValueError("That file isn't a readable video")
    data = json.loads(out.stdout or "{}")
    v = next((s for s in data.get("streams", []) if s.get("codec_type") == "video"), None)
    if not v or not v.get("width"):
        raise ValueError("No picture found in that file")
    w, h = int(v["width"]), int(v["height"])
    rot = abs(int(float((v.get("tags") or {}).get("rotate") or next(
        (d.get("rotation", 0) for d in v.get("side_data_list", []) if "rotation" in d), 0)))) % 360
    if rot in (90, 270):
        w, h = h, w
    num, _, den = (v.get("r_frame_rate") or "24/1").partition("/")
    fps = float(num) / float(den or 1) if float(den or 1) else 24.0
    return {"duration": round(float(data["format"]["duration"]), 3), "width": w, "height": h, "fps": round(fps, 2),
            "has_audio": any(s.get("codec_type") == "audio" for s in data.get("streams", []))}


def add(src: Path, original_name: str) -> dict:
    """Move an uploaded file into the library. Raises ValueError with a friendly message if unusable."""
    ext = Path(original_name).suffix.lower()
    if ext not in ALLOWED:
        raise ValueError(f"Unsupported file type {ext or '(none)'} — use MP4, MOV, M4V, WebM, MKV or AVI")
    meta = probe(src)
    if meta["duration"] < MIN_SECONDS:
        raise ValueError("That clip is shorter than 1 second")
    if meta["duration"] > MAX_SECONDS:
        raise ValueError(f"That clip is {meta['duration']:.0f} s long; keep clips under {MAX_SECONDS:.0f} s "
                         "(cut long videos into parts first)")
    cid = "c-" + uuid.uuid4().hex[:10]
    d = _dir(cid)
    d.mkdir(parents=True)
    shutil.move(str(src), d / f"original{ext}")
    meta.update(id=cid, name=Path(original_name).stem[:80], size=(d / f"original{ext}").stat().st_size,
                uploaded=int(time.time()), file=f"original{ext}")
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-ss", f"{meta['duration'] * 0.3:.2f}", "-i", str(d / meta["file"]),
                    "-frames:v", "1", "-vf", "scale=320:-2", "-q:v", "4", str(d / "thumb.jpg")], check=True)
    (d / "meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=1))
    return meta


def get(cid: str) -> dict:
    f = _dir(cid) / "meta.json"
    if not f.exists():
        raise KeyError(cid)
    return json.loads(f.read_text())


def list_clips() -> list[dict]:
    out = []
    for d in DIR.iterdir():
        if (d / "meta.json").exists():
            out.append(json.loads((d / "meta.json").read_text()))
    return sorted(out, key=lambda m: m["uploaded"])


def path_of(cid: str) -> Path:
    return _dir(cid) / get(cid)["file"]


def thumb_path(cid: str) -> Path:
    return _dir(cid) / "thumb.jpg"


def delete(cid: str) -> None:
    shutil.rmtree(_dir(cid), ignore_errors=True)


# ------------------------------------------------------------------------------- Gemini watches
def proxy(cid: str) -> Path:
    """Small silent copy for Gemini: 384 px tall, 5 fps, low bitrate (a 2-minute clip stays a few MB)."""
    out = _dir(cid) / "proxy.mp4"
    if not out.exists():
        subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", str(path_of(cid)), "-an", "-vf",
                        "scale=-2:384,fps=5", "-c:v", "libx264", "-preset", "veryfast", "-crf", "34", "-maxrate", "450k",
                        "-bufsize", "900k", "-pix_fmt", "yuv420p", str(out)], check=True)
    return out


def analyze(cid: str, ledger: CostLedger) -> dict:
    """What happens in the clip, in words a lyricist can use. Cached in the library, so reuse is free."""
    f = _dir(cid) / "analysis.json"
    if f.exists():
        ledger.text(f"watch clip {cid}", 0, 0, cached=True)
        return json.loads(f.read_text())
    m = get(cid)
    prompt = f"""CLIP ANALYSIS. You are helping a toddler (age 1-3) nursery-rhyme channel write lyrics that fit an existing short
video. Watch this clip (about {m['duration']:.0f} seconds, file name "{m['name']}") and describe it so that rhyming lyrics
can match it.
Return JSON: {{"summary": "1-2 simple sentences: what happens", "subjects": ["main characters / objects"],
"actions": ["what they do, in order"], "setting": "where it takes place", "mood": "happy | calm | playful | sleepy | ...",
"colours": ["main colours"], "moments": [{{"t": seconds from the start (number), "what": "short description"}}] (3 to 6
moments in time order), "has_text_or_logo": true or false, "kid_safe": true or false,
"kid_safe_notes": "if not suitable for toddlers (scary, violent, adult, loud, flashing...), say why"}}"""
    data = generate_json(prompt, ledger, f"watch clip {m['name']}", media=[proxy(cid)])
    data["moments"] = [x for x in (data.get("moments") or []) if isinstance(x, dict) and "t" in x][:8]
    f.write_text(json.dumps(data, ensure_ascii=False, indent=1))
    return data


# ------------------------------------------------------------------------------- segments
def plan_segments(clip_ids: list[str]) -> list[dict]:
    """Cut points: ~7 s per lyric line, never leaving a tiny stub; an odd count is made even (rhyming couplets) by
    splitting the longest piece when it is long enough."""
    segs: list[dict] = []
    for cid in clip_ids:
        m = get(cid)
        n = max(1, round(m["duration"] / SECONDS_PER_LINE))
        step = m["duration"] / n
        for k in range(n):
            segs.append({"clip": cid, "name": m["name"], "start": round(k * step, 3), "end": round((k + 1) * step, 3)})
    if len(segs) % 2 == 1 or len(segs) < 2:
        i = max(range(len(segs)), key=lambda j: segs[j]["end"] - segs[j]["start"])
        s = segs[i]
        if s["end"] - s["start"] >= 3.0:
            mid = round((s["start"] + s["end"]) / 2, 3)
            segs[i:i + 1] = [{**s, "end": mid}, {**s, "start": mid}]
    for s in segs:
        s["duration"] = round(s["end"] - s["start"], 3)
    return segs


def describe_segment(seg: dict, analysis: dict) -> str:
    """What the lyric for this segment should be about, from the clip analysis and the moments inside it."""
    inside = [f"at {x['t'] - seg['start']:.0f}s: {x.get('what', '')}" for x in analysis.get("moments", [])
              if seg["start"] - 0.01 <= float(x["t"]) <= seg["end"] + 0.01]
    bits = [analysis.get("summary", "")]
    if analysis.get("setting"):
        bits.append(f"Setting: {analysis['setting']}.")
    if analysis.get("subjects"):
        bits.append("Shows: " + ", ".join(map(str, analysis["subjects"][:4])) + ".")
    if inside:
        bits.append("In this part: " + "; ".join(inside) + ".")
    return " ".join(b for b in bits if b).strip()


def normalize_filter(vw: int, vh: int, W: int, H: int) -> str:
    """Fit any clip shape to the video frame: 16:9 is cropped to fill; other shapes (vertical phone video,
    square) are shown whole over a blurred copy of themselves instead of having their top and bottom cut off."""
    if abs((vw / vh) / (W / H) - 1) <= 0.08:
        return f"scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H},setsar=1"
    return (f"split[a][b];[a]scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H},boxblur=28:2,"
            f"eq=brightness=0.04:saturation=1.1[bg];[b]scale={W}:{H}:force_original_aspect_ratio=decrease[fg];"
            f"[bg][fg]overlay=(W-w)/2:(H-h)/2,setsar=1")


def cut_segment(cid: str, start: float, end: float, out: Path, W: int, H: int, fps: int) -> Path:
    """One piece of a clip: frame-accurate cut, silent, at the video's size and frame rate."""
    m = get(cid)
    n = max(1, round((end - start) * fps))
    vf = f"{normalize_filter(m['width'], m['height'], W, H)},fps={fps},setpts=N/{fps}/TB,trim=end_frame={n}"
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-ss", f"{start:.3f}", "-t", f"{end - start + 0.5:.3f}",
                    "-i", str(path_of(cid)), "-an", "-filter_complex", f"[0:v]{vf}[v]", "-map", "[v]", "-r", str(fps),
                    "-c:v", "libx264", "-preset", "veryfast", "-crf", "18", "-pix_fmt", "yuv420p", str(out)], check=True)
    return out
