"""Environment-driven settings. All values overridable via env vars."""
from __future__ import annotations

import functools
import os
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent


@dataclass(frozen=True)
class Settings:
    host: str
    port: int
    # Audio pipeline
    model_sr: int            # sample rate the model expects
    buffer_seconds: float    # ring buffer capacity per client
    window_sec: float        # inference window length
    hop_sec: float           # how often inference runs
    enroll_seconds: float    # clip length used for enrollment
    # Model / classification
    model_module: str | None
    conf_threshold: float    # min cosine-sim confidence to name a speaker
    silence_rms: float       # below this RMS a window counts as silence
    # Storage
    profiles_path: Path
    web_dir: Path
    clone_prepare_timeout_sec: float = 15.0  # optional clone warmup deadline

    @property
    def buffer_samples(self) -> int:
        return int(self.model_sr * self.buffer_seconds)

    @property
    def window_samples(self) -> int:
        return int(self.model_sr * self.window_sec)

    @property
    def enroll_samples(self) -> int:
        return int(self.model_sr * self.enroll_seconds)


@functools.lru_cache
def get_settings() -> Settings:
    env = os.environ
    return Settings(
        host=env.get("HOST", "0.0.0.0"),
        port=int(env.get("PORT", "3000")),
        model_sr=int(env.get("MODEL_SR", "16000")),
        buffer_seconds=float(env.get("BUFFER_SECONDS", "12")),
        window_sec=float(env.get("WINDOW_SEC", "2.5")),
        hop_sec=float(env.get("HOP_SEC", "0.5")),
        enroll_seconds=float(env.get("ENROLL_SECONDS", "10")),
        model_module=env.get("MODEL_MODULE") or None,
        conf_threshold=float(env.get("CONF_THRESHOLD", "0.55")),
        silence_rms=float(env.get("SILENCE_RMS", "0.005")),
        clone_prepare_timeout_sec=float(env.get(
            "CLONE_PREPARE_TIMEOUT_SEC", "15"
        )),
        profiles_path=Path(env.get(
            "PROFILES_PATH", str(REPO_ROOT / "server" / "data" / "profiles.json")
        )),
        web_dir=Path(env.get("WEB_DIR", str(REPO_ROOT / "web"))),
    )
