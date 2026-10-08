"""Animated on-screen lyrics designed for toddlers (and the parents singing along).

Look: big, round, bubbly "Baloo" letters, white with a thick candy-coloured outline and a soft
shadow, a different palette colour per line.  Motion: each line pops in with a little bounce,
and every word fills with sunshine yellow and does a small hop exactly while it is sung
(karaoke).  Nothing flashes or moves fast — gentle enough for 1-3 year olds.

Timing comes from the video's caption file (line start/end), so it works on videos made by
any version of the app; words share the line time in proportion to their length.
"""
from __future__ import annotations

import re
import subprocess
from pathlib import Path

from . import config

# language → (font family, right-to-left?)
FONTS = {
    "bn": "Baloo Da 2", "gu": "Baloo Bhai 2", "ml": "Baloo Chettan 2", "pa": "Baloo Paaji 2",
    "kn": "Baloo Tamma 2", "te": "Baloo Tammudu 2", "ta": "Baloo Thambi 2",
    "ur": "Noto Naskh Arabic", "ar": "Noto Naskh Arabic", "ja": "Noto Sans JP",
}
DEFAULT_FONT = "Baloo 2"  # Latin + Devanagari (English, Hindi, Marathi, German, French, Spanish, ...)
RTL = {"ur", "ar"}

# outline colours per line (ASS colours are &HBBGGRR)
LINE_COLOURS = ["&H00FF8F3A", "&H006B6BFF", "&H00D7A3FF", "&H0055B84B", "&H004DA9FF", "&H00FFA6C3"]
FILL_BEFORE = "&H00FFFFFF"   # white, not yet sung
FILL_SUNG = "&H003DD9FF"     # sunshine yellow once sung


def _ts(sec: float) -> str:
    sec = max(0.0, sec)
    h, r = divmod(sec, 3600)
    m, s = divmod(r, 60)
    return f"{int(h)}:{int(m):02}:{s:05.2f}"


def parse_srt(path: Path) -> list[tuple[float, float, str]]:
    def secs(t: str) -> float:
        h, m, rest = t.strip().split(":")
        s, ms = rest.split(",")
        return int(h) * 3600 + int(m) * 60 + int(s) + int(ms) / 1000
    out = []
    for block in re.split(r"\n\s*\n", path.read_text(encoding="utf-8").strip()):
        lines = block.strip().splitlines()
        if len(lines) >= 3 and "-->" in lines[1]:
            a, b = lines[1].split("-->")
            out.append((secs(a), secs(b), " ".join(lines[2:]).strip()))
    return out


def build_ass(cues: list[tuple[float, float, str]], lang: str, out: Path) -> Path:
    w, h = config.VIDEO_W, config.VIDEO_H
    size = round(h * 0.085)
    font = FONTS.get(lang, DEFAULT_FONT)
    header = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {w}
PlayResY: {h}
WrapStyle: 0
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Lyric,{font},{size},{FILL_SUNG},{FILL_BEFORE},&H00FF8F3A,&H64000000,1,0,0,0,100,100,0,0,1,{round(size * 0.13)},{round(size * 0.06)},2,{round(w * 0.06)},{round(w * 0.06)},{round(h * 0.05)},1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    events = []
    for i, (start, end, text) in enumerate(cues):
        words = text.split()
        if not words:
            continue
        sung = max(0.4, end - start - 0.3)            # caption end includes a 0.3 s tail
        weights = [max(1, len(wd)) for wd in words]
        total_w = sum(weights)
        appear = start - 0.25                          # line pops in just before it is sung
        offset_ms = 250                                # words start 250 ms after the line appears
        colour = LINE_COLOURS[i % len(LINE_COLOURS)]
        parts = []
        for wd, wt in zip(words, weights):
            dur_ms = round(sung * 1000 * wt / total_w)
            if lang in RTL:  # karaoke fill only; per-word scaling garbles right-to-left shaping
                parts.append(f"{{\\kf{dur_ms // 10}}}{wd}")
            else:
                t0 = offset_ms
                parts.append(f"{{\\kf{dur_ms // 10}\\t({t0},{t0 + 140},\\fscx116\\fscy116)"
                             f"\\t({t0 + 140},{t0 + 300},\\fscx100\\fscy100)}}{wd}")
            offset_ms += dur_ms
        pop = "\\fscx60\\fscy60\\t(0,180,\\fscx108\\fscy108)\\t(180,300,\\fscx100\\fscy100)"
        lead = f"{{\\fad(200,250)\\3c{colour}{pop}\\k25}}"
        events.append(f"Dialogue: 0,{_ts(appear)},{_ts(end + 0.35)},Lyric,,0,0,0,,{lead}{' '.join(parts)}")
    out.write_text(header + "\n".join(events) + "\n", encoding="utf-8")
    return out


def burn(video: Path, captions: Path, lang: str, out: Path) -> Path:
    """Re-encode the video with the animated lyrics drawn on; audio is copied untouched."""
    ass = build_ass(parse_srt(captions), lang, video.parent / f"lyrics_{lang}.ass")
    fonts = Path(config.FONTS_DIR)
    vf = f"ass={ass.name}" + (f":fontsdir={fonts.resolve()}" if fonts.exists() else "")
    subprocess.run(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-i", video.name, "-vf", vf,
                    "-c:v", "libx264", "-preset", "medium", "-crf", "19", "-pix_fmt", "yuv420p",
                    "-c:a", "copy", "-movflags", "+faststart", out.name],
                   check=True, cwd=video.parent)
    return out
