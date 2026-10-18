"""Voice model adapter.

The real voice-conversion model (a teammate's component) is plugged in by
setting MODEL_MODULE to a Python module exposing either a `load()` function
returning an object with these methods, or the functions directly:

`embed(pcm_f32_16k) -> 1-D embedding ndarray`              (enrollment clip)
`analyze_clip(pcm) -> {"f0": ...}`                          (optional)
`open_stream() -> {convert(pcm, target), set_effect(name)}` (optional; stateful
                                                           per-client converter)
`stream.prepare_target(target) -> None`                     (optional; blocking
                                                           clone preparation)
`convert(pcm_f32_16k, target|None) -> pcm_f32`              (stateless fallback)
`identify(pcm_f32_16k, profiles) -> (label, conf)`          (optional)

`target` is the profile dict {"emb": ndarray, "f0": float|None} of the voice
the user picked, or None.

Until the real model lands, VoiceChangerModel provides the full pipeline:
identification via spectral features + cosine similarity, and a real DSP
voice changer (pitch shift, robot, megaphone, F0-matched "clone").
"""
from __future__ import annotations

import importlib
import logging
import os
from typing import Protocol

import numpy as np

from .dsp import PitchShifter, RingMod, estimate_f0, megaphone

log = logging.getLogger("pi_voice.model")

_EPS = 1e-8
_BANDS = 8  # log-spaced band energies appended to the feature vector

EFFECTS = ("clean", "deep", "chipmunk", "robot", "megaphone", "clone")
_SHIFT_RATIOS = {"deep": 0.749, "chipmunk": 1.414}
_CLONE_CLAMP = (0.6, 1.7)   # sane bounds for F0-match ratio


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
    def convert(self, pcm: np.ndarray, target: dict | None) -> np.ndarray: ...
    def identify(self, pcm: np.ndarray,
                 profiles: dict[str, np.ndarray]) -> tuple[str, float]: ...


class VoiceStream:
    """Per-client stateful converter. Stateful because pitch shifting and
    ring modulation carry phase/history between chunks."""

    def __init__(self, sr: int):
        self.sr = sr
        self.shifter = PitchShifter(sr)
        self.ringmod = RingMod(sr)
        self.effect = "clean"
        self.ratio_ema = 1.0                    # smoothed clone ratio
        self.f0_buf = np.zeros(int(sr * 0.15), dtype=np.float32)  # 150 ms

    def set_effect(self, name: str) -> None:
        if name in EFFECTS:
            self.effect = name

    def convert(self, pcm: np.ndarray, target: dict | None) -> np.ndarray:
        pcm = np.asarray(pcm, dtype=np.float32)
        if pcm.size == 0:
            return pcm
        self.f0_buf = np.concatenate([self.f0_buf, pcm])[-self.f0_buf.size:]
        e = self.effect
        if e == "clean":
            return pcm
        if e == "robot":
            return self.ringmod.process(pcm)
        if e == "megaphone":
            return megaphone(pcm)
        if e == "clone":
            f0_t = target.get("f0") if target else None
            if f0_t:
                f0_in = estimate_f0(self.f0_buf, self.sr)
                if f0_in:
                    r = float(np.clip(f0_t / f0_in, *_CLONE_CLAMP))
                    self.ratio_ema = 0.8 * self.ratio_ema + 0.2 * r
            self.shifter.ratio = self.ratio_ema
            return self.shifter.process(pcm)
        self.shifter.ratio = _SHIFT_RATIOS.get(e, 1.0)
        return self.shifter.process(pcm)


class VoiceChangerModel:
    """Built-in model: spectral-feature identification + the DSP voice
    changer. Replaced wholesale by MODEL_MODULE when the real one lands."""

    def __init__(self, sr: int = 16000):
        self.sr = sr

    def embed(self, pcm: np.ndarray) -> np.ndarray:
        v = _features(pcm, self.sr)
        return v / (np.linalg.norm(v) + _EPS)

    def analyze_clip(self, pcm: np.ndarray) -> dict:
        return {"f0": estimate_f0(pcm, self.sr)}

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

    def open_stream(self) -> VoiceStream:
        return VoiceStream(self.sr)


def load_model() -> SpeakerModel:
    """Return the teammate's model if MODEL_MODULE is set, else the
    built-in voice changer."""
    module_name = os.environ.get("MODEL_MODULE")
    if module_name:
        try:
            mod = importlib.import_module(module_name)
            model = mod.load() if hasattr(mod, "load") else mod
            log.info("Loaded model module %s", module_name)
            return model
        except Exception:
            log.exception("MODEL_MODULE=%s failed to load; using built-in "
                          "voice changer", module_name)
    return VoiceChangerModel()
