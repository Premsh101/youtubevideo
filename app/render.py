"""ffmpeg assembly.

Visual track is rendered ONCE (silent), then muxed with each language's audio,
so EN + HI versions cost one render.  Scenes are joined with cross-fades and
continuous Ken-Burns motion so nothing looks cut or stitched.
"""
from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

from . import config, toddler
from .tts import media_duration

FF = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error"]


def _run(cmd: list[str]) -> None:
    subprocess.run(cmd, check=True)


# ------------------------------------------------------------------ per-scene clips
def _kenburns_expr(camera: str, frames: int, zoom: float = 0.16) -> str:
    z_in = f"1+{zoom}*on/{frames}"
    z_out = f"{1 + zoom}-{zoom}*on/{frames}"
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


def image_to_clip(image: Path, duration: float, camera: str, out: Path, zoom: float = 0.16) -> Path:
    frames = int(round(duration * config.FPS))
    vf = (
        f"scale=6000:-2,"  # upscale first so sub-pixel pans don't jitter
        f"zoompan={_kenburns_expr(camera, frames, zoom)}:d={frames}:s={config.VIDEO_W}x{config.VIDEO_H}:fps={config.FPS},"
        f"format=yuv420p"
    )
    _run(FF + ["-loop", "1", "-framerate", str(config.FPS), "-i", str(image), "-vf", vf,
               "-frames:v", str(frames), "-c:v", "libx264", "-preset", "veryfast", "-crf", "20", str(out)])
    return out


SMOOTH_MODE = os.getenv("SMOOTH_MODE", "mci")      # mci = optical-flow interpolation, blend = cheaper
MAX_STRETCH = float(os.getenv("MAX_STRETCH", "1.6"))   # beyond this, slow motion starts to look syrupy
INVISIBLE_STRETCH = 1.12                               # up to here plain retiming is imperceptible


def _duration(p: Path) -> float:
    return float(subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0",
                                 str(p)], capture_output=True, text=True, check=True).stdout.strip())


def fit_clip(clip: Path, duration: float, out: Path) -> Path:
    """Fit an AI video clip (Veo: 8 s) to the time its line needs — WITHOUT freezing.

    Holding the last frame (the old behaviour) makes the picture stop dead and restart at the
    next scene, which reads as a glitch. Instead we time-remap, like an editor would:
      * slightly too long  → speed up (≤ 1.25×) so the clip still ends on its final frame, which
                             is the first frame of the next scene (chained keyframes);
      * much too long      → trim;
      * a little too short → slow down ≤ 1.12× (plain retiming is invisible at that amount);
      * too short          → slow down up to 1.6× with motion-interpolated in-between frames;
      * even longer        → after 1.6×, keep moving: a gentle camera push on the last frame
                             continues seamlessly from where the clip ended. Never a frozen frame.
    """
    w, h, fps = config.VIDEO_W, config.VIDEO_H, config.FPS
    fit = f"scale={w}:{h}:force_original_aspect_ratio=increase,crop={w}:{h},setsar=1"
    src = _duration(clip)
    f = duration / src
    enc = ["-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-pix_fmt", "yuv420p", "-r", str(fps)]
    # trim + a tiny pad only absorb rounding (a few frames) — it sits inside the cross-fade to the next
    # scene, so it is never visible as a hold
    # frames are renumbered at a constant rate before trimming, so every scene is EXACTLY round(d×fps) frames
    # long — otherwise a frame or two lost per scene would add up and pull the audio out of sync
    n_frames = max(1, round(duration * fps))
    tail = f"tpad=stop_mode=clone:stop_duration=0.3,fps={fps},setpts=N/{fps}/TB,trim=end_frame={n_frames}"
    if f < 0.8:
        vf = f"{fit},setpts=PTS-STARTPTS,fps={fps},setpts=N/{fps}/TB,trim=end_frame={n_frames}"
    elif f <= INVISIBLE_STRETCH:
        vf = f"{fit},setpts=(PTS-STARTPTS)*{f:.5f},fps={fps},{tail}"
    else:
        stretch = min(f, MAX_STRETCH)
        interp = (f"minterpolate=fps={fps}:mi_mode=mci:mc_mode=aobmc:me_mode=bidir:vsbmc=1"
                  if SMOOTH_MODE == "mci" else f"minterpolate=fps={fps}:mi_mode=blend")
        if f <= MAX_STRETCH:
            vf = f"{fit},setpts=(PTS-STARTPTS)*{stretch:.5f},{interp},{tail}"
        else:
            slowed = out.with_name(out.stem + "_slow.mp4")
            _run(FF + ["-i", str(clip), "-an", "-vf", f"{fit},setpts=(PTS-STARTPTS)*{stretch:.5f},{interp}", *enc, str(slowed)])
            last = out.with_name(out.stem + "_last.png")
            _run(FF + ["-sseof", "-0.2", "-i", str(slowed), "-frames:v", "1", "-update", "1", str(last)])
            rest = duration - _duration(slowed)
            push = image_to_clip(last, max(rest, 2 / fps), "slow zoom in", out.with_name(out.stem + "_push.mp4"), zoom=0.06)
            _run(FF + ["-i", str(slowed), "-i", str(push), "-filter_complex",
                       f"[0:v]setsar=1[a];[1:v]setsar=1[b];[a][b]concat=n=2:v=1[c];[c]fps={fps},{tail}[v]", "-map", "[v]", *enc, str(out)])
            for t in (slowed, last, push):
                t.unlink(missing_ok=True)
            return out
    _run(FF + ["-i", str(clip), "-an", "-vf", vf, *enc, str(out)])
    return out


def freezes(video: Path, min_seconds: float = 0.5) -> list[dict]:
    """Quality check: moments where the picture doesn't change for ≥ min_seconds (looks 'hung').
    Calibrated so our slow camera moves don't count as frozen."""
    log = subprocess.run(["ffmpeg", "-hide_banner", "-i", str(video), "-map", "0:v", "-vf",
                          f"freezedetect=n=-60dB:d={min_seconds}", "-f", "null", "-"],
                         capture_output=True, text=True).stderr
    starts = [float(x) for x in re.findall(r"freeze_start: ([\d.]+)", log)]
    durs = [float(x) for x in re.findall(r"freeze_duration: ([\d.]+)", log)]
    out = []
    for i, st in enumerate(starts):
        d = durs[i] if i < len(durs) else None   # no duration logged = frozen until the very end
        out.append({"start": round(st, 2), "seconds": round(d, 2) if d is not None else None})
    return out


# ------------------------------------------------------------------ joining
def frame_exact(durations: list[float], xfade: float, fps: int) -> list[float]:
    """Snap scene BOUNDARIES (not each length) to whole frames. Rounding lengths one by one lets the
    error add up (audio slowly drifts); rounding the boundaries keeps every scene within half a frame
    of where its audio is, however long the video. `xfade` must already be whole frames."""
    exact = scene_starts(durations, xfade) + [scene_starts(durations, xfade)[-1] + durations[-1]]
    snapped = [round(t * fps) / fps for t in exact]
    out = [snapped[i + 1] - snapped[i] + xfade for i in range(len(durations) - 1)]
    out.append(snapped[-1] - snapped[-2])
    return [round(d * fps) / fps for d in out]


def scene_starts(durations: list[float], xfade: float) -> list[float]:
    """Where each scene begins (its cross-fade starts) in the joined video."""
    starts = [0.0]
    for d in durations[:-1]:
        starts.append(starts[-1] + d - xfade)
    return starts


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
            parts.append(f"{prev}[{i}:v]xfade=transition=fade:duration={xfade}:offset={starts[i]:.6f}{label}")
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
    voice_mix = "".join(f"[d{i}]" for i in range(n)) + f"amix=inputs={n}:normalize=0,alimiter=limit=0.95,apad,atrim=duration={total:.3f}[voice]"
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


def song_to_track(song: Path, total: float, out: Path) -> Path:
    """Sung mode: the song already contains vocals + instruments; just fit, fade and normalise."""
    _run(FF + ["-i", str(song), "-af",
               f"aformat=sample_rates=48000:channel_layouts=stereo,apad,atrim=duration={total:.3f},"
               f"afade=t=out:st={max(0, total - 2.5):.3f}:d=2.5,loudnorm=I=-16:TP=-1.5:LRA=11",
               "-ar", "48000", str(out)])
    return out


def make_vertical(src: Path, out: Path, duration: float) -> Path:
    """9:16 version for Reels / Shorts: the full 16:9 frame (with its lyrics) centred over a soft,
    blurred, slightly brightened copy of itself — nothing is cropped away. Cut to `duration` with a
    gentle fade so a long rhyme ends cleanly at a scene boundary."""
    w, h = 720, 1280
    fade = 1.2
    vf = (f"[0:v]split[a][b];"
          f"[a]scale={w}:{h}:force_original_aspect_ratio=increase,crop={w}:{h},boxblur=24:2,eq=brightness=0.06:saturation=1.2[bg];"
          f"[b]scale={w}:-2[fg];[bg][fg]overlay=(W-w)/2:(H-h)/2-60,"
          f"fade=t=out:st={max(0, duration - fade):.3f}:d={fade},format=yuv420p[v];"
          f"[0:a]afade=t=out:st={max(0, duration - fade):.3f}:d={fade}[au]")
    _run(FF + ["-i", str(src), "-t", f"{duration:.3f}", "-filter_complex", vf, "-map", "[v]", "-map", "[au]",
               "-c:v", "libx264", "-preset", "medium", "-crf", "20", "-r", str(config.FPS),
               "-c:a", "aac", "-b:a", "160k", "-ar", "48000", "-movflags", "+faststart", str(out)])
    return out


def bookend(main: Path, intro: Path | None, outro: Path | None, out: Path, music_seed: str) -> float:
    """intro + main + outro as one video. Clips are fitted to the main video's size/fps (letterboxed
    on white if their shape differs), the intro jingle is levelled to the song, and a silent outro
    gets a soft 4-second music tail. Returns the intro length (captions shift by this much)."""
    from .music import synth_lullaby
    w, h = (int(x) for x in subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=width,height", "-of", "csv=p=0",
         str(main)], capture_output=True, text=True, check=True).stdout.strip().split(",")[:2])

    def has_audio(p: Path) -> bool:
        return bool(subprocess.run(["ffprobe", "-v", "error", "-select_streams", "a", "-show_entries", "stream=index",
                                    "-of", "csv=p=0", str(p)], capture_output=True, text=True).stdout.strip())

    def dur(p: Path) -> float:
        return float(subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0",
                                     str(p)], capture_output=True, text=True, check=True).stdout.strip())

    parts = [p for p in (intro, main, outro) if p is not None]
    inputs: list[str] = []
    chains, labels = [], []
    vfit = (f"scale={w}:{h}:force_original_aspect_ratio=decrease,pad={w}:{h}:(ow-iw)/2:(oh-ih)/2:color=white,"
            f"fps={config.FPS},setsar=1,format=yuv420p")
    afit = "aresample=48000,aformat=sample_fmts=fltp:channel_layouts=stereo"
    for i, p in enumerate(parts):
        inputs += ["-i", str(p)]
    extra = len(parts)
    for i, p in enumerate(parts):
        chains.append(f"[{i}:v]{vfit}[v{i}]")
        d = dur(p)
        if has_audio(p):
            level = ",loudnorm=I=-18:TP=-2:LRA=11,aresample=48000" if p is not main else ""
            chains.append(f"[{i}:a]{afit}{level},apad,atrim=duration={d:.3f}[a{i}]")
        else:  # silent clip (the outro): soft music tail instead of dead air
            tail = out.parent / f"_tail_{i}.wav"
            synth_lullaby(max(2.0, d), f"{music_seed}-tail", tail)
            inputs += ["-i", str(tail)]
            chains.append(f"[{extra}:a]{afit},volume=-6dB,afade=t=in:d=0.4,"
                          f"afade=t=out:st={max(0, d - 1.2):.3f}:d=1.2,atrim=duration={d:.3f}[a{i}]")
            extra += 1
        labels.append(f"[v{i}][a{i}]")
    chains.append("".join(labels) + f"concat=n={len(parts)}:v=1:a=1[v][a]")
    _run(FF + inputs + ["-filter_complex", ";".join(chains), "-map", "[v]", "-map", "[a]",
                        "-c:v", "libx264", "-preset", "medium", "-crf", "19", "-c:a", "aac", "-b:a", "160k",
                        "-ar", "48000", "-movflags", "+faststart", str(out)])
    for t in out.parent.glob("_tail_*.wav"):
        t.unlink()
    return dur(intro) if intro is not None else 0.0


def shift_srt(src: Path, offset: float, out: Path) -> Path:
    """Captions for the full (intro + rhyme + outro) video: every cue moves later by `offset`."""
    import re

    def fmt(t: float) -> str:
        h, r = divmod(t, 3600)
        m, s = divmod(r, 60)
        return f"{int(h):02}:{int(m):02}:{int(s):02},{int(round((s % 1) * 1000)) % 1000:03}"

    def shift(mt):
        h, m, s, ms = (int(x) for x in mt.groups())
        return fmt(h * 3600 + m * 60 + s + ms / 1000 + offset)
    out.write_text(re.sub(r"(\d+):(\d+):(\d+),(\d+)", shift, src.read_text(encoding="utf-8")), encoding="utf-8")
    return out


def mux(video: Path, audio: Path, out: Path) -> Path:
    _run(FF + ["-i", str(video), "-i", str(audio), "-map", "0:v", "-map", "1:a", "-c:v", "copy",
               "-c:a", "aac", "-b:a", "160k", "-movflags", "+faststart", str(out)])
    # no -shortest: it stops at the audio buffer's edge and silently drops the last few video frames;
    # both tracks are already exactly the same length
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


__all__ = ["song_to_track", "snap_to_bars", "image_to_clip", "fit_clip", "crossfade_concat", "build_audio", "mux", "write_srt",
           "thumbnail", "media_duration"]
