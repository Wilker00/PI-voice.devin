"""Audio helpers: PCM16 conversion, resampling, and a numpy ring buffer."""
from __future__ import annotations

import numpy as np


def pcm16_to_float32(data: bytes) -> np.ndarray:
    """Little-endian Int16 PCM bytes -> float32 in [-1, 1]."""
    return np.frombuffer(data, dtype="<i2").astype(np.float32) / 32768.0


def float32_to_pcm16(x: np.ndarray) -> bytes:
    x = np.clip(x, -1.0, 1.0)
    return (x * 32767.0).astype("<i2").tobytes()


def resample_linear(x: np.ndarray, src_sr: int, dst_sr: int) -> np.ndarray:
    """Linear-interpolation resample. Cheap and good enough on a Pi 4."""
    if src_sr == dst_sr or x.size == 0:
        return x.astype(np.float32, copy=False)
    n_out = int(round(x.size * dst_sr / src_sr))
    if n_out <= 0:
        return np.empty(0, dtype=np.float32)
    xp = np.linspace(0.0, 1.0, num=x.size, endpoint=False)
    xi = np.linspace(0.0, 1.0, num=n_out, endpoint=False)
    return np.interp(xi, xp, x).astype(np.float32)


class StreamResampler:
    """Chunk-continuous resampler for one audio stream.

    resample_linear() treats every chunk as standalone, which leaves seams
    at chunk boundaries and aliases when downsampling. This keeps the
    interpolation phase and filter history between calls, and low-passes
    below the target Nyquist before decimating (e.g. 48k -> 16k).
    """

    def __init__(self, src_sr: int, dst_sr: int, taps: int = 31):
        self.src_sr, self.dst_sr = src_sr, dst_sr
        self.step = src_sr / dst_sr      # input samples per output sample
        self._pos = 1.0                  # next output position; index 0 = _last
        self._last = 0.0
        self._fir = None
        if dst_sr < src_sr:
            cutoff = 0.45 * dst_sr / src_sr     # cycles/sample, under Nyquist
            n = np.arange(taps) - (taps - 1) / 2
            h = 2 * cutoff * np.sinc(2 * cutoff * n) * np.hamming(taps)
            self._fir = (h / h.sum()).astype(np.float32)
            self._hist = np.zeros(taps - 1, dtype=np.float32)

    def process(self, x: np.ndarray) -> np.ndarray:
        x = np.asarray(x, dtype=np.float32)
        if self.src_sr == self.dst_sr or x.size == 0:
            return x
        if self._fir is not None:
            padded = np.concatenate([self._hist, x])
            self._hist = padded[-self._hist.size:]
            x = np.convolve(padded, self._fir, mode="valid").astype(np.float32)
        buf = np.concatenate([[self._last], x])
        end = buf.size - 1
        if self._pos > end:
            self._pos -= x.size
            self._last = float(buf[-1])
            return np.empty(0, dtype=np.float32)
        idx = np.arange(self._pos, end + 1e-9, self.step)
        out = np.interp(idx, np.arange(buf.size), buf).astype(np.float32)
        self._pos = idx[-1] + self.step - end
        self._last = float(buf[-1])
        return out


class RingBuffer:
    """Fixed-capacity circular buffer of float32 samples.

    Writes past capacity overwrite the oldest audio; read_last(n) returns
    the most recent n samples in chronological order.
    """

    def __init__(self, capacity: int):
        self._buf = np.zeros(capacity, dtype=np.float32)
        self.capacity = capacity
        self._pos = 0          # next write index
        self._filled = 0       # total samples written (capped at capacity)

    def write(self, samples: np.ndarray) -> None:
        samples = np.asarray(samples, dtype=np.float32)
        n = samples.size
        if n == 0:
            return
        if n >= self.capacity:  # keep only the tail that fits
            samples = samples[-self.capacity:]
            n = samples.size
            self._buf[:] = samples
            self._pos = 0
            self._filled = self.capacity
            return
        end = self._pos + n
        if end <= self.capacity:
            self._buf[self._pos:end] = samples
        else:
            first = self.capacity - self._pos
            self._buf[self._pos:] = samples[:first]
            self._buf[:end - self.capacity] = samples[first:]
        self._pos = end % self.capacity
        self._filled = min(self._filled + n, self.capacity)

    def read_last(self, n: int) -> np.ndarray:
        """Most recent n samples (fewer if the buffer isn't that full yet)."""
        n = min(n, self._filled)
        if n == 0:
            return np.empty(0, dtype=np.float32)
        start = (self._pos - n) % self.capacity
        if start + n <= self.capacity:
            return self._buf[start:start + n].copy()
        first = self.capacity - start
        return np.concatenate([self._buf[start:], self._buf[:n - first]])

    def __len__(self) -> int:
        return self._filled
