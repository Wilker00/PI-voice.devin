"""Lightweight DSP for the voice changer — numpy only, Pi-4 friendly.

- PitchShifter: classic dual-reader delay-line shifter, vectorized.
- estimate_f0:  autocorrelation F0 tracker (used for clone-mode matching).
- RingMod / megaphone: extra presets.
"""
from __future__ import annotations

import numpy as np


def estimate_f0(x: np.ndarray, sr: int,
                fmin: float = 50.0, fmax: float = 600.0) -> float | None:
    """Median-free autocorrelation F0 estimate. Returns Hz or None."""
    x = np.asarray(x, dtype=np.float32)
    if x.size < sr // 10:
        return None
    x = x - x.mean()
    if float(np.sqrt(np.mean(x ** 2))) < 1e-3:
        return None
    n = x.size
    nfft = 1 << (2 * n - 1).bit_length()
    spec = np.fft.rfft(x, nfft)
    ac = np.fft.irfft(spec * np.conj(spec), nfft)[:n]
    if ac[0] <= 0:
        return None
    ac = ac / (ac[0] + 1e-9)
    lo, hi = int(sr / fmax), min(int(sr / fmin), n - 1)
    if hi <= lo:
        return None
    seg = ac[lo:hi]
    if seg.max() < 0.3:          # unvoiced / too aperiodic
        return None
    return sr / (int(np.argmax(seg)) + lo)


class PitchShifter:
    """Delay-line pitch shifter: two readers sweeping a circular buffer at
    `ratio` samples-per-output-sample, Hann-crossfaded so a reader's gain is
    ~0 at its wrap point. Change `ratio` freely — readers adapt live."""

    def __init__(self, sr: int, ratio: float = 1.0, delay_sec: float = 0.05):
        self.L = int(sr * delay_sec)
        self.buf = np.zeros(self.L, dtype=np.float32)
        self.w = 0                          # write head
        self.rd = [0.0, self.L / 2.0]       # reader phases
        self.ratio = float(ratio)

    def process(self, x: np.ndarray) -> np.ndarray:
        x = np.asarray(x, dtype=np.float32)
        n = x.size
        if n == 0:
            return x
        tail = x[-self.L:] if n > self.L else x   # only last L samples fit
        end = self.w + tail.size
        if end <= self.L:
            self.buf[self.w:end] = tail
        else:
            first = self.L - self.w
            self.buf[self.w:] = tail[:first]
            self.buf[:end - self.L] = tail[first:]
        self.w = end % self.L

        t = np.arange(n, dtype=np.float32)
        out = np.zeros(n, dtype=np.float32)
        for r in range(2):
            pos = (self.rd[r] + self.ratio * t) % self.L
            self.rd[r] = (self.rd[r] + self.ratio * n) % self.L
            i0 = pos.astype(np.int32)
            frac = pos - i0
            i1 = (i0 + 1) % self.L
            v = self.buf[i0] * (1.0 - frac) + self.buf[i1] * frac
            g = 0.5 - 0.5 * np.cos(2.0 * np.pi * pos / self.L)
            out += v * g
        return out


class RingMod:
    """Robot/Dalek effect: multiply by a low-freq sine carrier (phase-
    continuous across chunks)."""

    def __init__(self, sr: int, freq: float = 30.0):
        self.sr, self.freq, self.phase = sr, freq, 0.0

    def process(self, x: np.ndarray) -> np.ndarray:
        n = x.size
        ph = self.phase + 2.0 * np.pi * self.freq * np.arange(n) / self.sr
        self.phase = (self.phase + 2.0 * np.pi * self.freq * n
                      / self.sr) % (2.0 * np.pi)
        return (x * np.sin(ph)).astype(np.float32)


def megaphone(x: np.ndarray) -> np.ndarray:
    """Cheap 'loudspeaker' coloration: soft-clip grit + 5-tap smoothing."""
    y = np.tanh(2.5 * np.asarray(x, dtype=np.float32))
    y = np.convolve(y, np.ones(5) / 5.0, mode="same")
    return (0.8 * y).astype(np.float32)
