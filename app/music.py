"""Zero-cost background music.

If the user drops royalty-free tracks into data/music/, one is picked (deterministic
per title).  Otherwise a gentle pentatonic lullaby is synthesised with numpy:
soft bell melody + warm pad chords, 92 BPM, major key — the register and tempo
toddlers find soothing.  Cached by (seed, duration).
"""
from __future__ import annotations

import hashlib
import wave
from pathlib import Path

import numpy as np

from . import config, moods, toddler

SR = 44100


def _bell(freq: float, dur: float, sr: int = SR) -> np.ndarray:
    t = np.linspace(0, dur, int(sr * dur), endpoint=False)
    env = np.exp(-3.2 * t)
    return env * (np.sin(2 * np.pi * freq * t) + 0.35 * np.sin(2 * np.pi * freq * 2 * t)
                  + 0.12 * np.sin(2 * np.pi * freq * 3 * t))


def _pad(freqs: list[float], dur: float, sr: int = SR) -> np.ndarray:
    t = np.linspace(0, dur, int(sr * dur), endpoint=False)
    attack = np.minimum(1.0, t / 0.6)
    release = np.minimum(1.0, (dur - t) / 0.8)
    env = attack * release
    sig = sum(np.sin(2 * np.pi * f * t) + 0.3 * np.sin(2 * np.pi * f * 2.005 * t) for f in freqs)
    return env * sig / (len(freqs) * 1.3)


def synth_lullaby(seconds: float, seed: str, out: Path, mood: str = "playful") -> Path:
    rng = np.random.default_rng(int(hashlib.md5(seed.encode()).hexdigest()[:8], 16))
    m, md = toddler.MUSIC, moods.get(mood)
    beat = 60.0 / md["bpm"]
    root = m["key_root_hz"]
    scale = m["scale"]
    n_total = int(SR * (seconds + 2))
    mix = np.zeros(n_total)

    # I - vi - IV - V style progression (in semitones from root)
    chords = [[0, 4, 7], [9, 12, 16], [5, 9, 12], [7, 11, 14]]
    bar = beat * 4
    pos = 0.0
    ci = 0
    while pos < seconds:
        chord = chords[ci % len(chords)]
        freqs = [root / 2 * 2 ** (s / 12) for s in chord]
        seg = _pad(freqs, bar)
        s0 = int(pos * SR)
        mix[s0:s0 + len(seg)] += 0.22 * seg[: n_total - s0]
        # melody: 4 bell notes per bar on the beat, stepwise random walk over pentatonic
        deg = int(rng.integers(0, len(scale)))
        per_bar = md["melody_notes"]
        for b in range(per_bar):
            deg = int(np.clip(deg + rng.integers(-1, 2), 0, len(scale) - 1))
            octave = 1 if rng.random() < 0.8 else 2
            f = root * octave * 2 ** (scale[deg] / 12)
            note = _bell(f, beat * (4 / per_bar) * 1.6)
            n0 = int((pos + b * (4 / per_bar) * beat) * SR)
            if n0 < n_total and rng.random() < 0.85:
                mix[n0:n0 + len(note)] += 0.18 * note[: n_total - n0]
        pos += bar
        ci += 1

    mix = mix[: int(SR * seconds)]
    fade = int(SR * 2.5)
    mix[-fade:] *= np.linspace(1, 0, fade)
    mix[:fade] *= np.linspace(0, 1, fade)
    mix = np.clip(mix / (np.max(np.abs(mix)) + 1e-6) * 0.8, -1, 1)
    pcm = (mix * 32767).astype("<i2")
    stereo = np.repeat(pcm[:, None], 2, axis=1)
    with wave.open(str(out), "wb") as wf:
        wf.setnchannels(2)
        wf.setsampwidth(2)
        wf.setframerate(SR)
        wf.writeframes(stereo.tobytes())
    return out


def get_music(seconds: float, seed: str, mood: str = "playful") -> Path:
    tracks = sorted(p for p in config.MUSIC_DIR.iterdir() if p.suffix.lower() in {".mp3", ".wav", ".m4a", ".ogg"})
    if tracks:   # your own tracks: one whose file name contains the mood (e.g. sleepy_music_box.mp3) is preferred
        tracks = [t for t in tracks if mood in t.stem.lower()] or tracks
        return tracks[int(hashlib.md5(seed.encode()).hexdigest()[:4], 16) % len(tracks)]
    key = hashlib.md5(f"{seed}|{int(seconds)}{'' if mood == 'playful' else '|' + mood}".encode()).hexdigest()[:16]
    out = config.CACHE_DIR / "music" / f"{key}.wav"
    if out.exists():
        return out
    out.parent.mkdir(parents=True, exist_ok=True)
    return synth_lullaby(seconds, seed, out, mood)
