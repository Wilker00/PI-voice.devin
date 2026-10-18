"""Lightweight DSP for the voice changer — numpy only, Pi-4 friendly.

- PitchShifter: streaming WSOLA (stretch, then read back ratio x faster).
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
    """Streaming WSOLA pitch shifter: time-stretch by `ratio`, then read
    the stretched stream `ratio`x faster — same duration, pitch x ratio.

    WSOLA overlap-adds 24 ms Hann frames at a fixed synthesis hop; each
    frame is taken from within ±6 ms of its nominal input position, at the
    offset whose waveform best continues the previous frame (normalized
    cross-correlation). Splices land in phase, so there is none of the
    periodic cancellation ("warble") of a two-tap delay-line shifter.

    process() returns exactly as many samples as it is given: an output
    queue primed with WSOLA's lookahead in silence absorbs the bursty
    frame production. Latency is a constant 50 ms at 16 kHz, and the
    output is identical however the input is chunked.
    """

    def __init__(self, sr: int, ratio: float = 1.0, frame_sec: float = 0.024):
        self.N = max(64, int(sr * frame_sec)) & ~1   # frame (even)
        self.hs = self.N // 2                        # synthesis hop
        self.delta = int(sr * 0.006)                 # splice search ±6 ms
        self.win = (0.5 - 0.5 * np.cos(2 * np.pi * np.arange(self.N)
                                       / self.N)).astype(np.float32)
        self.ratio = float(ratio)
        # WSOLA stage (input domain)
        self.buf = np.zeros(0, dtype=np.float32)
        self.off = 0                         # absolute index of buf[0]
        self.apos = float(self.delta)        # next nominal analysis position
        self.template: np.ndarray | None = None
        self.ola = np.zeros(self.N, dtype=np.float32)
        # anti-alias + reader stage (stretched domain)
        self.taps = 31
        self.fir_hist = np.zeros(self.taps - 1, dtype=np.float32)
        self.s = np.zeros(0, dtype=np.float32)
        self.spos = 0.0
        # Output queue. Production never trails input by more than the WSOLA
        # lookahead (2·delta + N + hs), whatever the chunking or ratio, so
        # priming with that much silence means it can never run dry.
        self.latency = 2 * self.delta + self.N + self.hs + 32
        self.fifo = np.zeros(self.latency, dtype=np.float32)
        self.underruns = 0                   # gaps; stays 0 by construction

    def _stretch(self) -> np.ndarray:
        N, hs, dl = self.N, self.hs, self.delta
        out = []
        while True:
            a = int(round(self.apos))
            if a + dl + N + hs > self.off + self.buf.size:
                break                                  # need more input
            b = a - self.off
            if self.template is None:
                k = 0
            else:
                region = self.buf[b - dl:b + dl + N]
                c = np.correlate(region, self.template, mode="valid")
                e = np.cumsum(np.concatenate([[0.0], region * region]))
                c = c / np.sqrt(e[N:] - e[:-N] + 1e-9)
                k = int(np.argmax(c)) - dl
            st = b + k
            self.ola += self.buf[st:st + N] * self.win
            out.append(self.ola[:hs].copy())
            self.ola = np.concatenate([self.ola[hs:],
                                       np.zeros(hs, dtype=np.float32)])
            self.template = self.buf[st + hs:st + hs + N].copy()
            self.apos += hs / self.ratio
        drop = int(self.apos) - dl - self.off - 1
        if drop > 0:
            self.buf = self.buf[drop:]
            self.off += drop
        return np.concatenate(out) if out else np.zeros(0, dtype=np.float32)

    def _lowpass(self, y: np.ndarray) -> np.ndarray:
        # Reading `ratio`x faster folds content above Nyquist/ratio back down.
        if y.size == 0:   # np.convolve "valid" would swap args and emit junk
            return y
        cutoff = min(0.45, 0.45 / max(self.ratio, 1e-3))  # cycles/sample
        m = np.arange(self.taps) - (self.taps - 1) / 2
        h = 2 * cutoff * np.sinc(2 * cutoff * m) * np.hamming(self.taps)
        padded = np.concatenate([self.fir_hist, y])
        self.fir_hist = padded[-(self.taps - 1):]
        return np.convolve(padded, (h / h.sum()).astype(np.float32),
                           mode="valid").astype(np.float32)

    def _read(self) -> np.ndarray:
        """Read the stretched stream `ratio`x faster (linear interp)."""
        # Emit only positions whose i1 tap exists; the next append makes the
        # boundary sample readable, so chunking can't change the output.
        avail = self.s.size - 2 - self.spos
        if avail < 0:
            return np.zeros(0, dtype=np.float32)
        pos = self.spos + self.ratio * np.arange(int(avail / self.ratio) + 1)
        i0 = pos.astype(np.int64)
        frac = (pos - i0).astype(np.float32)
        i1 = np.minimum(i0 + 1, self.s.size - 1)
        y = self.s[i0] * (1.0 - frac) + self.s[i1] * frac
        self.spos = float(pos[-1] + self.ratio)
        cut = int(self.spos)
        self.s, self.spos = self.s[cut:], self.spos - cut
        return y.astype(np.float32)

    def process(self, x: np.ndarray) -> np.ndarray:
        x = np.asarray(x, dtype=np.float32)
        n = x.size
        if n == 0:
            return x
        self.buf = np.concatenate([self.buf, x])
        self.s = np.concatenate([self.s, self._lowpass(self._stretch())])
        self.fifo = np.concatenate([self.fifo, self._read()])
        if self.fifo.size < n:                 # can't happen; never block
            self.underruns += 1
            self.fifo = np.concatenate(
                [self.fifo, np.zeros(n - self.fifo.size, np.float32)])
        out, self.fifo = self.fifo[:n], self.fifo[n:]
        return out


def estimate_f0_median(x: np.ndarray, sr: int, frame_sec: float = 0.1,
                       hop_sec: float = 0.05) -> float | None:
    """Typical F0 of a longer clip: median over voiced frames.

    One autocorrelation over a whole 10 s enrollment clip blurs the pitch
    movement and pauses together and often finds no clear peak (None).
    """
    x = np.asarray(x, dtype=np.float32)
    n, hop = int(sr * frame_sec), int(sr * hop_sec)
    f0s = [f for i in range(0, max(1, x.size - n + 1), hop)
           if (f := estimate_f0(x[i:i + n], sr)) is not None]
    return float(np.median(f0s)) if len(f0s) >= 3 else estimate_f0(x, sr)


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
