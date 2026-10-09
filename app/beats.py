"""Beat grid for animation: when does the music hit?

* Our own synthesised lullaby has a known tempo and starts exactly on the first beat.
* A song from ElevenLabs (or a track you dropped in data/music) has an unknown tempo, so it is measured:
  an energy-onset curve is autocorrelated for the tempo (70-130 BPM, kid-music range) and the beat phase
  is the offset where a comb of beats lines up best with the onsets.  Pure numpy, no extra install.
"""
from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path

import numpy as np

SR = 11025
HOP = 110          # 100 envelope frames per second


@dataclass
class Grid:
    bpm: float
    offset: float          # time (s) of a beat, in the audio's own clock
    method: str = "known"

    @property
    def period(self) -> float:
        return 60.0 / self.bpm

    def phase(self, t: float) -> tuple[float, int]:
        """(position inside the current beat 0..1, index of the current beat) at audio time t."""
        x = (t - self.offset) / self.period
        n = int(np.floor(x))
        return x - n, n


def _decode(path: Path, max_seconds: float = 150.0) -> np.ndarray:
    raw = subprocess.run(["ffmpeg", "-v", "error", "-i", str(path), "-t", str(max_seconds), "-ac", "1", "-ar", str(SR),
                          "-f", "s16le", "-"], capture_output=True, check=True).stdout
    return np.frombuffer(raw, dtype="<i2").astype(np.float32) / 32768.0


def _onset_curve(x: np.ndarray) -> np.ndarray:
    n = (len(x) - 1024) // HOP
    if n < 200:
        raise ValueError("audio too short to find a beat")
    win = np.hanning(1024).astype(np.float32)
    spec = np.empty((n, 513), np.float32)
    for i in range(n):
        spec[i] = np.abs(np.fft.rfft(x[i * HOP:i * HOP + 1024] * win))
    logspec = np.log1p(spec * 30)
    flux = np.maximum(0, np.diff(logspec, axis=0)).sum(axis=1)       # spectral flux = "something new started"
    flux = np.concatenate([[0], flux])
    flux -= np.convolve(flux, np.ones(50) / 50, mode="same")         # remove slow trends
    return np.maximum(flux, 0)


def detect(path: Path, lo: float = 70, hi: float = 130) -> Grid:
    on = _onset_curve(_decode(path))
    fps = SR / HOP
    ac = np.correlate(on, on, mode="full")[len(on) - 1:]
    best_bpm, best_score = 92.0, -1.0
    for bpm in np.arange(lo, hi + 0.25, 0.25):
        lag = fps * 60.0 / bpm
        i = int(round(lag))
        if i + 1 >= len(ac):
            continue
        # tempo strength: autocorrelation at 1, 2 and 4 beats (musical structure repeats at multiples)
        score = sum(np.interp(lag * m, np.arange(len(ac)), ac) / (1 + 0.15 * (m - 1)) for m in (1, 2, 4))
        if score > best_score:
            best_bpm, best_score = float(bpm), float(score)
    period = fps * 60.0 / best_bpm
    # phase: the offset whose beat comb collects the most onset energy
    best_off, best_val = 0.0, -1.0
    for off in np.arange(0, period, 0.5):
        idx = np.arange(off, len(on) - 1, period).astype(int)
        val = on[idx].sum()
        if val > best_val:
            best_off, best_val = float(off), float(val)
    # envelope frame i describes the 1024-sample window that STARTS at i*HOP; an onset registers when it is
    # near the window's middle, so shift by half a window (~46 ms) to get the true beat time
    offset = (best_off / fps + 512 / SR) % (60.0 / best_bpm)
    return Grid(bpm=best_bpm, offset=offset, method="detected")
