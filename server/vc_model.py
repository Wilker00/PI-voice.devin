"""PI Voice real-model adapter (MODEL_MODULE drop-in).

Contract (see server/model.py):

    load() -> VCModel
    model.embed(pcm_f32_16k)   -> "embedding" (here: the raw enrollment clip,
                                   level-normalized — zero-shot VC needs the
                                   reference *audio*, not a vector)
    model.convert(pcm, target) -> converted pcm_f32_16k; **empty while
                                   buffering** (the session layer drops
                                   empty converts, which gives us chunked
                                   utterance semantics for free)
    model.identify(pcm, profs) -> (label, conf)  # HUD nicety only

Design: real VC models can't convert ~85 ms chunks in isolation, so
convert() buffers audio, segments it into utterances with a lightweight RMS
VAD (hangover + preroll + min/max length), and on end-of-utterance runs the
whole clip through the backend — audio.cpp's MeanVC2, zero-shot, using the
enrollment clip as --voice-ref. The converted utterance returns as one block
and the phone's playback scheduler plays it in order.

Env config (all optional):
    VC_BACKEND=mock|audiocpp     default: audiocpp if AUDIOCPP_* set, else mock
    AUDIOCPP_BIN / AUDIOCPP_MODEL      required for the real backend
    AUDIOCPP_FAMILY (meanvc2), AUDIOCPP_THREADS (3),
    AUDIOCPP_TIMEOUT_S (120), AUDIOCPP_EXTRA_ARGS ("")
    VC_HANGOVER_MS (350), VC_MIN_UTT_MS (350), VC_MAX_UTT_S (6),
    VC_PREROLL_MS (150), VC_SILENCE_RMS (0.006), VC_MIN_REF_S (2.0)
    VC_STATS_FILE  optional per-utterance JSON sidecar (rtf etc. — cat it on
                   the Pi during the demo)

Single-phone assumption: one client session. A lock serializes access so a
second concurrent client cannot corrupt the segmenter (audio would interleave
but stays sane).
"""
from __future__ import annotations

import json
import logging
import os
import queue
import shlex
import subprocess
import tempfile
import threading
import time
import wave
from pathlib import Path

import numpy as np

from .audio import resample_linear
from .model import _EPS, _features

log = logging.getLogger("pi_voice.vc_model")


# ---------------------------------------------------------------------------
# WAV helpers (stdlib only — no soundfile/scipy dependency on the Pi)
# ---------------------------------------------------------------------------

def write_wav(path, pcm: np.ndarray, sr: int) -> None:
    x = np.clip(np.asarray(pcm, np.float32), -1.0, 1.0)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes(np.round(x * 32767.0).astype("<i2").tobytes())


def read_wav(path) -> tuple[np.ndarray, int]:
    with wave.open(str(path), "rb") as w:
        sr = w.getframerate()
        raw = w.readframes(w.getnframes())
    return np.frombuffer(raw, dtype="<i2").astype(np.float32) / 32767.0, sr


# ---------------------------------------------------------------------------
# Segmenter: RMS VAD with hangover, preroll, min/max utterance length
# ---------------------------------------------------------------------------

class Segmenter:
    """Chunk in, complete utterances out. Feed() never blocks.

    - 32 ms RMS frames decide voiced/silence.
    - Utterance starts on speech onset (with a preroll of recent silence)
      and ends after `hangover_ms` of trailing silence — the trailing
      silence (up to the hangover) is kept in the utterance for natural
      playback.
    - `max_utt_s` force-emits mid-speech so latency stays bounded during
      long monologues; the next segment continues with a short carryover.
    - While idle only a preroll window is retained (memory bounded).
    """

    def __init__(self, sr: int = 16000, frame_ms: float = 32,
                 hangover_ms: float = 350, min_utt_ms: float = 350,
                 max_utt_s: float = 6.0, preroll_ms: float = 150,
                 silence_rms: float = 0.006):
        self.sr = int(sr)
        self.frame = max(1, int(self.sr * frame_ms / 1000))
        self.hangover = int(self.sr * hangover_ms / 1000)
        self.min_utt = int(self.sr * min_utt_ms / 1000)
        self.max_utt = int(self.sr * max_utt_s)
        self.preroll = int(self.sr * preroll_ms / 1000)
        self.silence_rms = float(silence_rms)
        # state
        self._frames: list[np.ndarray] = []   # current utterance audio
        self._total = 0                        # samples in _frames
        self._voiced = 0                       # voiced samples in _frames
        self._silence_run = 0                  # trailing silence samples
        self._in_speech = False
        self._preroll_buf = np.empty(0, np.float32)

    # -- internals ----------------------------------------------------------

    def _emit(self, carry: int) -> list[np.ndarray]:
        """Fold current frames into utterance(s); keep `carry` samples of the
        tail as the head of the next segment. Drops utterances whose voiced
        part is under `min_utt`."""
        out: list[np.ndarray] = []
        utt = (np.concatenate(self._frames) if self._frames
               else np.empty(0, np.float32))
        voiced, self._voiced = self._voiced, 0
        self._total = 0
        self._silence_run = 0
        if voiced >= self.min_utt:
            out.append(utt)
        if carry > 0 and utt.size >= carry:
            self._frames = [utt[-carry:]]
            self._total = carry
        else:
            self._frames = []
        return out

    # -- public -------------------------------------------------------------

    def feed(self, pcm: np.ndarray) -> list[np.ndarray]:
        """Append a chunk; return zero or more complete utterances."""
        pcm = np.asarray(pcm, np.float32)
        outs: list[np.ndarray] = []
        i = 0
        n = pcm.size
        while i < n:
            fr = pcm[i:i + self.frame]
            i += fr.size
            rms = float(np.sqrt(np.mean(fr.astype(np.float64) ** 2))) \
                if fr.size else 0.0
            if rms >= self.silence_rms:
                if not self._in_speech:
                    self._frames.append(self._preroll_buf)
                    self._total += self._preroll_buf.size
                    self._preroll_buf = np.empty(0, np.float32)
                    self._in_speech = True
                self._frames.append(fr)
                self._total += fr.size
                self._voiced += fr.size
                self._silence_run = 0
                if self._total >= self.max_utt:      # bound the latency
                    outs += self._emit(carry=self.preroll)
                    # mid-speech: keep going, no preroll re-priming
            else:
                if self._in_speech:
                    self._frames.append(fr)
                    self._total += fr.size
                    self._silence_run += fr.size
                    if self._silence_run >= self.hangover:
                        outs += self._emit(carry=0)
                        self._in_speech = False
                else:
                    # idle: keep only the most recent preroll of silence
                    if self.preroll:
                        tail = np.concatenate([self._preroll_buf, fr])
                        self._preroll_buf = np.ascontiguousarray(
                            tail[-self.preroll:], np.float32)
        return outs


# ---------------------------------------------------------------------------
# Backends
# ---------------------------------------------------------------------------

class MockBackend:
    """Identity conversion (with optional gain) — used by the tests, and as
    the pre-binary fallback so the app runs end-to-end before audio.cpp
    lands on the Pi."""

    name = "mock"

    def __init__(self, gain: float = 1.0):
        self.gain = float(gain)

    def convert(self, utt: np.ndarray, ref: np.ndarray) -> np.ndarray | None:
        return np.asarray(utt, np.float32) * self.gain


class AudiocppCliBackend:
    """One `audiocpp_cli` process per utterance (zero-shot, reference clip
    via --voice-ref). The model reloads per call; once the RTF bench says
    that overhead matters, the planned upgrade is a resident
    `audiocpp_server` backend."""

    name = "audiocpp_cli"

    def __init__(self, bin: str, model: str, sr: int = 16000,
                 family: str = "meanvc2", threads: int = 3,
                 timeout_s: float = 120.0, extra_args: str = ""):
        self.bin = str(bin)
        self.model = str(model)
        self.sr = int(sr)
        self.family = family
        self.threads = int(threads)
        self.timeout_s = float(timeout_s)
        self.extra_args = shlex.split(extra_args) if extra_args else []
        self._lock = threading.Lock()

    def convert(self, utt: np.ndarray, ref: np.ndarray) -> np.ndarray | None:
        with self._lock, tempfile.TemporaryDirectory(prefix="pivc-") as td:
            d = Path(td)
            in_wav, ref_wav, out_wav = d / "in.wav", d / "ref.wav", d / "out.wav"
            try:
                write_wav(in_wav, utt, self.sr)
                write_wav(ref_wav, ref, self.sr)
            except Exception:
                log.exception("wav write failed")
                return None
            cmd = [self.bin, "--task", "vc", "--family", self.family,
                   "--model", self.model, "--audio", str(in_wav),
                   "--voice-ref", str(ref_wav), "--out", str(out_wav),
                   "--backend", "cpu", "--threads", str(self.threads),
                   "--metrics", *self.extra_args]
            t0 = time.perf_counter()
            try:
                proc = subprocess.run(cmd, capture_output=True, text=True,
                                      timeout=self.timeout_s)
            except subprocess.TimeoutExpired:
                log.error("audiocpp_cli timed out after %.0fs", self.timeout_s)
                return None
            except Exception:
                log.exception("audiocpp_cli failed to launch (bin=%s)", self.bin)
                return None
            wall = time.perf_counter() - t0
            if proc.returncode != 0 or not out_wav.exists():
                log.error("audiocpp_cli rc=%s\nstdout tail:\n%s\nstderr tail:\n%s",
                          proc.returncode, proc.stdout[-1500:], proc.stderr[-1500:])
                return None
            try:
                y, out_sr = read_wav(out_wav)
            except Exception:
                log.exception("reading converted wav failed")
                return None
            if y.size == 0:
                log.warning("audiocpp_cli produced empty output")
                return None
            if out_sr != self.sr:
                y = resample_linear(y, out_sr, self.sr)
            log.info("audiocpp: utt=%.2fs wall=%.2fs rtf=%.2f",
                     utt.size / self.sr, wall, wall / (utt.size / self.sr))
            return y


# ---------------------------------------------------------------------------
# The model
# ---------------------------------------------------------------------------

class VCModel:
    """SpeakerModel-protocol implementation. embed() smuggles the raw
    enrollment clip through the JSON profile store; convert() buffers to
    utterance granularity before calling the backend."""

    def __init__(self, sr: int = 16000, backend=None,
                 min_ref_s: float = 2.0, stats_file: str | None = None,
                 passthrough_no_target: bool = True, **seg_kw):
        self.sr = int(sr)
        self.backend = backend or MockBackend()
        self.seg_kw = dict(seg_kw)
        self.seg = Segmenter(sr=self.sr, **seg_kw)
        self.min_ref = int(self.sr * float(min_ref_s))
        self.stats_file = Path(stats_file) if stats_file else None
        self.passthrough_no_target = bool(passthrough_no_target)
        self._lock = threading.Lock()
        self._feat_cache: dict[int, tuple[int, np.ndarray]] = {}
        self.last_stats: dict = {}

    # -- enrollment ----------------------------------------------------------

    def embed(self, pcm: np.ndarray) -> np.ndarray:
        clip = np.asarray(pcm, np.float32).copy()
        peak = float(np.max(np.abs(clip))) if clip.size else 0.0
        if 1e-4 < peak < 0.9:
            clip *= 0.9 / peak          # healthy-level reference for the model
        return clip                      # stored as-is, handed back as target

    # -- conversion -----------------------------------------------------------

    def convert(self, pcm: np.ndarray, target: np.ndarray | None = None) -> np.ndarray:
        pcm = np.asarray(pcm, np.float32)
        ref = None
        if target is not None:
            if isinstance(target, dict):
                target = target.get("emb")
        if target is not None:
            ref = np.asarray(target, np.float32)
        if ref is None or ref.size < self.min_ref:
            # No usable target selected — their UI calls this "converting raw".
            return pcm if self.passthrough_no_target else np.empty(0, np.float32)
        outs: list[np.ndarray] = []
        with self._lock:
            utts = self.seg.feed(pcm)
            for utt in utts:
                t0 = time.perf_counter()
                y = self.backend.convert(utt, ref)
                wall = time.perf_counter() - t0
                utt_s = utt.size / self.sr
                self.last_stats = {
                    "backend": getattr(self.backend, "name", "?"),
                    "utt_s": round(utt_s, 2),
                    "convert_s": round(wall, 2),
                    "rtf": round(wall / utt_s, 2) if utt_s > 0 else None,
                    "ts": round(time.time(), 1),
                }
                self._write_stats(self.last_stats)
                if y is not None and y.size:
                    outs.append(np.asarray(y, np.float32))
        if not outs:
            return np.empty(0, np.float32)
        return outs[0] if len(outs) == 1 else np.concatenate(outs)

    # -- HUD nicety: who is talking -------------------------------------------

    def identify(self, pcm: np.ndarray,
                 profiles: dict[str, np.ndarray]) -> tuple[str, float]:
        if not profiles:
            return "unknown", 0.0
        v = _features(pcm, self.sr)
        vn = np.linalg.norm(v)
        if vn < _EPS:
            return "unknown", 0.0
        v = v / (vn + _EPS)
        best, best_sim = "unknown", 0.0
        for name, emb in profiles.items():
            f = self._profile_features(np.asarray(emb, np.float32))
            if f is None:
                continue
            sim = float(np.dot(v, f))
            if sim > best_sim:
                best, best_sim = name, sim
        return best, float(np.clip(best_sim, 0.0, 1.0))

    def _profile_features(self, emb: np.ndarray) -> np.ndarray | None:
        """Toy spectral features of a stored *clip*, cached by array identity
        (a few ms of FFT once per profile, not per classify hop)."""
        key = id(emb)
        hit = self._feat_cache.get(key)
        if hit is not None and hit[0] == emb.size:
            return hit[1]
        if emb.size < 256:            # legacy stub feature-vectors: unusable
            return None
        f = _features(emb, self.sr)
        fn = np.linalg.norm(f)
        if fn < _EPS:
            return None
        f = f / (fn + _EPS)
        if len(self._feat_cache) > 8:
            self._feat_cache.clear()
        self._feat_cache[key] = (emb.size, f)
        return f

    # -- stats sidecar ---------------------------------------------------------

    def _write_stats(self, stats: dict) -> None:
        if not self.stats_file:
            return
        try:
            tmp = self.stats_file.with_suffix(".tmp")
            tmp.write_text(json.dumps(stats))
            os.replace(tmp, self.stats_file)
        except Exception:
            log.exception("stats sidecar write failed")


    def open_stream(self) -> "CloneStream":
        """Non-blocking per-client clone stream — the session prefers this
        when present (see server/session.py: open_stream protocol)."""
        return CloneStream(self)


# ---------------------------------------------------------------------------
# Non-blocking clone stream (priority fix: recv never waits on inference)
# ---------------------------------------------------------------------------

class CloneStream:
    """Per-client non-blocking clone converter.

    convert() NEVER runs the backend on the caller's thread: completed
    utterances go to a bounded worker queue (drop-oldest, so lag cannot
    compound) and finished conversions come back FIFO on later calls.
    The phone streams chunks continuously (silence included), so results
    flush within ~100 ms of completion.
    """

    def __init__(self, model: "VCModel"):
        self.model = model
        self.sr = model.sr
        self.backend = model.backend
        self.seg = Segmenter(sr=model.sr, **model.seg_kw)
        self.min_ref = model.min_ref
        self.passthrough = model.passthrough_no_target
        self.depth = max(1, int(os.environ.get("VC_QUEUE_DEPTH", "2")))
        self._in: "queue.Queue" = queue.Queue()
        self._out: "queue.Queue" = queue.Queue()
        self._gen = 0          # bumped when leaving clone: stale work dies
        self.dropped = 0
        self._thread = threading.Thread(target=self._worker, daemon=True)
        self._thread.start()

    # -- session hooks ------------------------------------------------------

    def set_effect(self, name: str) -> None:
        # Leaving clone: everything queued or in flight was produced for the
        # old transform (maybe an old target) — it must never reach the ears.
        if name != "clone":
            self._gen += 1
            self._flush()

    def close(self) -> None:
        """Session teardown: let the worker thread exit."""
        self._in.put(None)

    def _flush(self) -> None:
        for q in (self._in, self._out):
            while True:
                try:
                    q.get_nowait()
                except queue.Empty:
                    break

    def prepare_target(self, target) -> None:
        """End-to-end warmup/health probe (runs inside the session's prepare
        task with its own timeout): proves the backend path works so the
        session's fallback-to-character UX triggers correctly when it doesn't."""
        ref = self._ref_of(target)
        if ref is None:
            return
        y = self.backend.convert(np.zeros(int(self.sr * 0.5), np.float32), ref)
        if y is None:
            raise RuntimeError("clone backend failed warmup probe")

    # -- hot path -------------------------------------------------------------

    def convert(self, pcm: np.ndarray, target) -> np.ndarray:
        pcm = np.asarray(pcm, np.float32)
        ref = self._ref_of(target)
        if ref is None:
            return pcm if self.passthrough else np.empty(0, np.float32)
        for utt in self.seg.feed(pcm):
            while self._in.qsize() >= self.depth:   # stay live: drop oldest
                try:
                    self._in.get_nowait()
                    self.dropped += 1
                    log.info("clone queue full — dropped oldest utterance")
                except queue.Empty:
                    break
            self._in.put((utt, ref, self._gen))
        outs = []
        while True:
            try:
                outs.append(self._out.get_nowait())
            except queue.Empty:
                break
        if not outs:
            return np.empty(0, np.float32)
        return outs[0] if len(outs) == 1 else np.concatenate(outs)

    # -- worker ---------------------------------------------------------------

    def _worker(self) -> None:
        while True:
            item = self._in.get()
            if item is None:
                return                      # close(): session is gone
            utt, ref, gen = item
            if gen != self._gen:
                continue                    # stale: dropped by an effect change
            t0 = time.perf_counter()
            try:
                y = self.backend.convert(utt, ref)
            except Exception:
                log.exception("clone worker conversion failed")
                continue
            wall = time.perf_counter() - t0
            utt_s = utt.size / self.sr
            self.model._write_stats({
                "backend": getattr(self.backend, "name", "?"),
                "utt_s": round(utt_s, 2),
                "convert_s": round(wall, 2),
                "rtf": round(wall / utt_s, 2) if utt_s > 0 else None,
                "queue_dropped": self.dropped,
                "ts": round(time.time(), 1),
            })
            # gen may have bumped mid-conversion: check again before emitting
            if gen == self._gen and y is not None and y.size:
                self._out.put(np.asarray(y, np.float32))

    def _ref_of(self, target):
        if isinstance(target, dict):
            target = target.get("emb")
        if target is None:
            return None
        ref = np.asarray(target, np.float32)
        return ref if ref.size >= self.min_ref else None


# ---------------------------------------------------------------------------
# Entry point used by server.model.load_model() via MODEL_MODULE
# ---------------------------------------------------------------------------

def load() -> VCModel:
    sr = int(os.environ.get("MODEL_SR", "16000"))
    seg_kw = dict(
        frame_ms=float(os.environ.get("VC_FRAME_MS", "32")),
        hangover_ms=float(os.environ.get("VC_HANGOVER_MS", "350")),
        min_utt_ms=float(os.environ.get("VC_MIN_UTT_MS", "350")),
        max_utt_s=float(os.environ.get("VC_MAX_UTT_S", "6")),
        preroll_ms=float(os.environ.get("VC_PREROLL_MS", "150")),
        silence_rms=float(os.environ.get("VC_SILENCE_RMS", "0.006")),
    )
    kind = os.environ.get("VC_BACKEND", "").strip().lower()
    binp = os.environ.get("AUDIOCPP_BIN", "").strip()
    modelp = os.environ.get("AUDIOCPP_MODEL", "").strip()
    if kind == "mock":
        backend: MockBackend | AudiocppCliBackend = MockBackend()
    elif binp and modelp:
        backend = AudiocppCliBackend(
            bin=binp, model=modelp, sr=sr,
            family=os.environ.get("AUDIOCPP_FAMILY", "meanvc2"),
            threads=int(os.environ.get("AUDIOCPP_THREADS", "3")),
            timeout_s=float(os.environ.get("AUDIOCPP_TIMEOUT_S", "120")),
            extra_args=os.environ.get("AUDIOCPP_EXTRA_ARGS", ""))
    else:
        log.warning("VC_MODEL: audio.cpp not configured "
                    "(set AUDIOCPP_BIN + AUDIOCPP_MODEL, or VC_BACKEND=mock) "
                    "— using passthrough mock; converted audio = raw voice")
        backend = MockBackend()
    m = VCModel(
        sr=sr, backend=backend, **seg_kw,
        min_ref_s=float(os.environ.get("VC_MIN_REF_S", "2.0")),
        stats_file=os.environ.get("VC_STATS_FILE") or None,
        passthrough_no_target=os.environ.get("VC_PASSTHROUGH_NO_TARGET", "1") == "1",
    )
    log.info("VC_MODEL ready: backend=%s sr=%d max_utt=%.1fs",
             backend.name, sr, seg_kw["max_utt_s"])
    return m
