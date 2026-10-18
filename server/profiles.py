"""Enrolled-voice profile store: name -> embedding, JSON-persisted."""
from __future__ import annotations

import json
import threading
from pathlib import Path

import numpy as np


class ProfileStore:
    def __init__(self, path: Path):
        self.path = Path(path)
        self._lock = threading.Lock()
        self._emb: dict[str, np.ndarray] = {}
        self.load()

    def load(self) -> None:
        with self._lock:
            if self.path.exists():
                try:
                    raw = json.loads(self.path.read_text())
                    self._emb = {
                        name: np.asarray(vec, dtype=np.float32)
                        for name, vec in raw.items()
                    }
                except Exception:
                    self._emb = {}

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(
            {n: e.tolist() for n, e in self._emb.items()}
        ))

    def add(self, name: str, embedding: np.ndarray) -> None:
        with self._lock:
            self._emb[name] = np.asarray(embedding, dtype=np.float32)
            self._save()

    def remove(self, name: str) -> bool:
        with self._lock:
            existed = self._emb.pop(name, None) is not None
            if existed:
                self._save()
            return existed

    def embeddings(self) -> dict[str, np.ndarray]:
        with self._lock:
            return dict(self._emb)

    def names(self) -> list[str]:
        with self._lock:
            return sorted(self._emb)
