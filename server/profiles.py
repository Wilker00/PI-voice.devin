"""Enrolled-voice profile store: name -> {embedding, f0}, JSON-persisted."""
from __future__ import annotations

import json
import threading
from pathlib import Path

import numpy as np


class ProfileStore:
    def __init__(self, path: Path):
        self.path = Path(path)
        self._lock = threading.Lock()
        self._data: dict[str, dict] = {}   # name -> {"emb": ndarray, "f0": float|None}
        self.load()

    def load(self) -> None:
        with self._lock:
            if not self.path.exists():
                return
            try:
                raw = json.loads(self.path.read_text())
            except Exception:
                return
            self._data = {}
            for name, val in raw.items():
                if isinstance(val, list):          # legacy: bare embedding
                    self._data[name] = {"emb": np.asarray(val, dtype=np.float32),
                                        "f0": None}
                else:
                    self._data[name] = {
                        "emb": np.asarray(val["emb"], dtype=np.float32),
                        "f0": val.get("f0"),
                    }

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(
            {n: {"emb": d["emb"].tolist(), "f0": d["f0"]}
             for n, d in self._data.items()}
        ))

    def add(self, name: str, embedding: np.ndarray,
            f0: float | None = None) -> None:
        with self._lock:
            self._data[name] = {"emb": np.asarray(embedding, dtype=np.float32),
                                "f0": f0}
            self._save()

    def get(self, name: str) -> dict | None:
        with self._lock:
            return self._data.get(name)

    def remove(self, name: str) -> bool:
        with self._lock:
            existed = self._data.pop(name, None) is not None
            if existed:
                self._save()
            return existed

    def embeddings(self) -> dict[str, np.ndarray]:
        with self._lock:
            return {n: d["emb"] for n, d in self._data.items()}

    def names(self) -> list[str]:
        with self._lock:
            return sorted(self._data)
