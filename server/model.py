"""Speaker-model adapter.

The real model (a teammate's component) is plugged in by setting MODEL_MODULE
to a Python module that exposes either a `load()` function returning an object
with `embed(pcm)` / `identify(pcm, profiles)`, or those functions directly.

`embed(pcm_f32_16k)  -> 1-D embedding ndarray`
`identify(pcm_f32_16k, profiles: dict[str, ndarray]) -> (label, confidence)`

Until then, StubModel exercises the full pipeline with numpy spectral
features and cosine-similarity matching.
"""
from __future__ import annotations

import importlib
import logging
import os
from typing import Protocol

import numpy as np

log = logging.getLogger("pi_voice.model")

_EPS = 1e-8
_BANDS = 8  # log-spaced band energies appended to the feature vector


def _features(pcm: np.ndarray, sr: int) -> np.ndarray:
    """Lightweight voice-ish fingerprint: RMS, ZCR, spectral centroid,
    and log-band energies of the magnitude spectrum."""
    if pcm.size < 256:
        return np.zeros(2 + _BANDS, dtype=np.float32)
    pcm = pcm - np.mean(pcm)
    rms = float(np.sqrt(np.mean(pcm ** 2)))
    zcr = float(np.mean(np.abs(np.diff(np.signbit(pcm)).astype(np.float32))))
    win = np.hanning(pcm.size).astype(np.float32)
    mag = np.abs(np.fft.rfft(pcm * win)) + _EPS
    freqs = np.fft.rfftfreq(pcm.size, 1.0 / sr)
    centroid = float(np.sum(freqs * mag) / np.sum(mag)) / (sr / 2.0)
    # log-spaced band energies across 0..sr/2
    edges = np.geomspace(20, max(21, sr / 2.0), _BANDS + 1)
    band_e = np.array([
        np.sum(mag[(freqs >= lo) & (freqs < hi)] ** 2)
        for lo, hi in zip(edges[:-1], edges[1:])
    ], dtype=np.float32)
    band_e = np.log1p(band_e)
    if band_e.max() > 0:
        band_e = band_e / band_e.max()
    return np.concatenate([[rms * 10.0, zcr, centroid], band_e]).astype(np.float32)


class SpeakerModel(Protocol):
    def embed(self, pcm: np.ndarray) -> np.ndarray: ...
    def identify(self, pcm: np.ndarray,
                 profiles: dict[str, np.ndarray]) -> tuple[str, float]: ...


class StubModel:
    """Placeholder speaker-ID model: cosine similarity over _features()."""

    def __init__(self, sr: int = 16000):
        self.sr = sr

    def embed(self, pcm: np.ndarray) -> np.ndarray:
        v = _features(pcm, self.sr)
        return v / (np.linalg.norm(v) + _EPS)

    def identify(self, pcm: np.ndarray,
                 profiles: dict[str, np.ndarray]) -> tuple[str, float]:
        if not profiles:
            return "unknown", 0.0
        v = self.embed(pcm)
        best_name, best_sim = "unknown", 0.0
        for name, emb in profiles.items():
            emb = np.asarray(emb, dtype=np.float32)
            sim = float(np.dot(v, emb) /
                        ((np.linalg.norm(v) + _EPS) * (np.linalg.norm(emb) + _EPS)))
            if sim > best_sim:
                best_name, best_sim = name, sim
        return best_name, max(0.0, min(1.0, best_sim))


def load_model() -> SpeakerModel:
    """Return the teammate's model if MODEL_MODULE is set, else StubModel."""
    module_name = os.environ.get("MODEL_MODULE")
    if module_name:
        try:
            mod = importlib.import_module(module_name)
            model = mod.load() if hasattr(mod, "load") else mod
            log.info("Loaded model module %s", module_name)
            return model
        except Exception:
            log.exception("MODEL_MODULE=%s failed to load; using StubModel",
                          module_name)
    return StubModel()
