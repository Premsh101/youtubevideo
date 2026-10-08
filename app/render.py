"""ffmpeg assembly.

Visual track is rendered ONCE (silent), then muxed with each language's audio,
so EN + HI versions cost one render.  Scenes are joined with cross-fades and
continuous Ken-Burns motion so nothing looks cut or stitched.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

from . import config, toddler
from .tts import media_duration

FF = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error"]


def _run(cmd: list[str]) -> None:
    subprocess.run(cmd, check=True)


# ------------------------------------------------------------------ per-scene clips
def _kenburns_expr(camera: str, frames: int) -> str:
    z_in = f"1+0.16*on/{frames}"
    z_out = f"1.16-0.16*on/{frames}"
    centre = "x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)'"
    if "zoom out" in camera:
        return f"z='{z_out}':{centre}"
    if "pan left" in camera:
        return f"z='1.14':x='(iw-iw/zoom)*(1-on/{frames})':y='ih/2-(ih/zoom/2)'"
    if "pan right" in camera:
        return f"z='1.14':x='(iw-iw/zoom)*on/{frames}':y='ih/2-(ih/zoom/2)'"
    return f"z='{z_in}':{centre}"


def snap_to_bars(durations: list[float], xfade: float, bpm: int) -> list[float]:
    """Stretch each scene so every lyric starts on a music bar (like a sung rhyme, not narration)."""
    bar = 60.0 / bpm * 4
    out, t = [], 0.0
    for d in durations:
        end = t + d - xfade
        snapped = (int(end / bar) + 1) * bar
        out.append(snapped - t + xfade)
        t = snapped
    return out


def image_to_clip(image: Path, duration: float, camera: str, out: Path) -> Path:
    frames = int(round(duration * config.FPS))
    vf = (
        f"scale=6000:-2,"  # upscale first so sub-pixel pans don't jitter
        f"zoompan={_kenburns_expr(camera, frames)}:d={frames}:s={config.VIDEO_W}x{config.VIDEO_H}:fps={config.FPS},"
        f"format=yuv420p"
    )
    _run(FF + ["-loop", "1", "-framerate", str(config.FPS), "-i", str(image), "-vf", vf,
               "-frames:v", str(frames), "-c:v", "libx264", "-preset", "veryfast", "-crf", "20", str(out)])
    return out


def fit_clip(clip: Path, duration: float, out: Path) -> Path:
    """Scale a Veo clip to project size; hold the last frame if the narration runs longer."""
    vf = (f"scale={config.VIDEO_W}:{config.VIDEO_H}:force_original_aspect_ratio=increase,"
          f"crop={config.VIDEO_W}:{config.VIDEO_H},fps={config.FPS},"
          f"tpad=stop_mode=clone:stop_duration=30,trim=duration={duration:.3f},setpts=PTS-STARTPTS,format=yuv420p")
    _run(FF + ["-i", str(clip), "-an", "-vf", vf, "-c:v", "libx264", "-preset", "veryfast", "-crf", "20", str(out)])
    return out


# ------------------------------------------------------------------ joining
def crossfade_concat(clips: list[Path], durations: list[float], xfade: float, out: Path) -> list[float]:
    """Join clips with xfade. Returns the start time of each scene in the final timeline."""
    n = len(clips)
    starts = [0.0]
    for i in range(1, n):
        starts.append(starts[-1] + durations[i - 1] - xfade)
    total = starts[-1] + durations[-1]

    inputs: list[str] = []
    for c in clips:
        inputs += ["-i", str(c)]
    if n == 1:
        fc = f"[0:v]fade=t=in:d=1,fade=t=out:st={total - 1.5:.3f}:d=1.5[v]"
    else:
        parts = []
        prev = "[0:v]"
        for i in range(1, n):
            label = "[v]" if i == n - 1 else f"[x{i}]"
            parts.append(f"{prev}[{i}:v]xfade=transition=fade:duration={xfade}:offset={starts[i]:.3f}{label}")
            prev = label
        parts[-1] = parts[-1][:-3] + "[xx]"
        parts.append(f"[xx]fade=t=in:d=1,fade=t=out:st={total - 1.5:.3f}:d=1.5[v]")
        fc = ";".join(parts)
    _run(FF + inputs + ["-filter_complex", fc, "-map", "[v]", "-c:v", "libx264", "-preset", "medium",
                        "-crf", "19", "-pix_fmt", "yuv420p", "-movflags", "+faststart", str(out)])
    return starts


# ------------------------------------------------------------------ audio
def build_audio(voice_lines: list[dict], starts: list[float], total: float, music: Path, out: Path,
                lead_in: float = 0.6) -> Path:
    """Voice lines placed at scene starts, music ducked underneath, loudness-normalised."""
    inputs = ["-i", str(music)]
    delays = []
    for i, v in enumerate(voice_lines):
        inputs += ["-i", v["path"]]
        ms = int((starts[i] + lead_in) * 1000)
        if v.get("chorus"):
            # cheap "children singing along" effect: two detuned copies (+3 / +5 semitones) under the lead
            delays.append(
                f"[{i + 1}:a]aformat=sample_rates=48000:channel_layouts=stereo,asplit=3[l{i}][h1{i}][h2{i}];"
                f"[h1{i}]asetrate=48000*1.189,aresample=48000,atempo=1/1.189,volume=-7dB,adelay=35|35[hh1{i}];"
                f"[h2{i}]asetrate=48000*1.335,aresample=48000,atempo=1/1.335,volume=-10dB,adelay=60|60[hh2{i}];"
                f"[l{i}][hh1{i}][hh2{i}]amix=inputs=3:normalize=0,adelay={ms}|{ms}[d{i}]")
        else:
            delays.append(f"[{i + 1}:a]aformat=sample_rates=48000:channel_layouts=stereo,adelay={ms}|{ms}[d{i}]")
    n = len(voice_lines)
    voice_mix = "".join(f"[d{i}]" for i in range(n)) + f"amix=inputs={n}:normalize=0,alimiter=limit=0.95[voice]"
    duck = toddler.MUSIC["duck_db"]
    fc = ";".join(delays + [
        voice_mix,
        f"[0:a]aformat=sample_rates=48000:channel_layouts=stereo,aloop=loop=-1:size=2e9,atrim=duration={total:.3f},"
        f"volume={duck}dB,afade=t=out:st={max(0, total - 3):.3f}:d=3[music]",
        "[voice]asplit[v1][v2]",
        "[music][v2]sidechaincompress=threshold=0.02:ratio=6:attack=40:release=700[ducked]",
        "[ducked][v1]amix=inputs=2:normalize=0,loudnorm=I=-16:TP=-1.5:LRA=11[a]",
    ])
    _run(FF + inputs + ["-filter_complex", fc, "-map", "[a]", "-t", f"{total:.3f}", "-ar", "48000", str(out)])
    return out


def mux(video: Path, audio: Path, out: Path) -> Path:
    _run(FF + ["-i", str(video), "-i", str(audio), "-map", "0:v", "-map", "1:a", "-c:v", "copy",
               "-c:a", "aac", "-b:a", "160k", "-shortest", "-movflags", "+faststart", str(out)])
    return out


def write_srt(voice_lines: list[dict], starts: list[float], out: Path, lead_in: float = 0.6) -> Path:
    def ts(t: float) -> str:
        h, r = divmod(t, 3600)
        m, s = divmod(r, 60)
        return f"{int(h):02}:{int(m):02}:{int(s):02},{int((s % 1) * 1000):03}"
    lines = []
    for i, v in enumerate(voice_lines):
        a = starts[i] + lead_in
        lines.append(f"{i + 1}\n{ts(a)} --> {ts(a + v['duration'] + 0.3)}\n{v['text']}\n")
    out.write_text("\n".join(lines), encoding="utf-8")
    return out


def thumbnail(video: Path, out: Path, at: float = 3.0) -> Path:
    _run(FF + ["-ss", f"{at:.2f}", "-i", str(video), "-frames:v", "1", "-q:v", "2", str(out)])
    return out


__all__ = ["snap_to_bars", "image_to_clip", "fit_clip", "crossfade_concat", "build_audio", "mux", "write_srt",
           "thumbnail", "media_duration"]
