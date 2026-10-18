"""Per-WebSocket client session: audio ingest, inference loop, enrollment."""
from __future__ import annotations

import asyncio
import json
import logging
import time

import numpy as np
from starlette.websockets import WebSocket

from .audio import (RingBuffer, float32_to_pcm16, pcm16_to_float32,
                    resample_linear)
from .config import Settings
from .profiles import ProfileStore

log = logging.getLogger("pi_voice.session")


class ClientSession:
    def __init__(self, ws: WebSocket, model, profiles: ProfileStore,
                 settings: Settings):
        self.ws = ws
        self.model = model
        self.profiles = profiles
        self.s = settings
        self.ring = RingBuffer(settings.buffer_samples)
        self.src_rate = settings.model_sr  # until client reports otherwise
        self.enrolling: str | None = None
        self.target_name: str | None = None
        self.target: dict | None = None   # {"emb": ndarray, "f0": float|None}
        # Prefer a stateful per-client stream when the model offers one.
        self.conv = (model.open_stream()
                     if hasattr(model, "open_stream") else model)
        self._stop = asyncio.Event()

    async def run(self) -> None:
        recv = asyncio.create_task(self._recv_loop())
        infer = asyncio.create_task(self._infer_loop())
        done, pending = await asyncio.wait(
            (recv, infer), return_when=asyncio.FIRST_COMPLETED)
        for t in pending:
            t.cancel()
        await asyncio.gather(*pending, return_exceptions=True)

    # -- inbound -----------------------------------------------------

    async def _recv_loop(self) -> None:
        while not self._stop.is_set():
            msg = await self.ws.receive()
            if msg["type"] == "websocket.disconnect":
                return
            if (data := msg.get("bytes")) is not None:
                await self._on_pcm(data)
            elif (text := msg.get("text")) is not None:
                try:
                    await self._on_control(json.loads(text))
                except (ValueError, KeyError) as exc:
                    await self._send({"type": "error", "message": str(exc)})

    async def _on_pcm(self, data: bytes) -> None:
        pcm = pcm16_to_float32(data)
        if self.src_rate != self.s.model_sr:
            pcm = resample_linear(pcm, self.src_rate, self.s.model_sr)
        self.ring.write(pcm)
        # Per-chunk conversion keeps the speaker loop low-latency.
        # While enrolling we buffer only — no playback to feed back.
        convert = getattr(self.conv, "convert", None)
        if convert is None or self.enrolling:
            return
        out = await asyncio.to_thread(convert, pcm, self.target)
        if out is None or out.size == 0:
            return
        if self.src_rate != self.s.model_sr:
            out = resample_linear(out, self.s.model_sr, self.src_rate)
        try:
            await self.ws.send_bytes(float32_to_pcm16(out))
        except Exception:
            self._stop.set()

    async def _on_control(self, msg: dict) -> None:
        kind = msg.get("type")
        if kind == "start":
            self.src_rate = int(msg.get("sampleRate") or self.s.model_sr)
            await self._send({"type": "started", "model_sr": self.s.model_sr})
        elif kind == "enroll_start":
            name = str(msg.get("name") or "").strip()
            if not name:
                raise ValueError("enroll_start requires a name")
            self.enrolling = name
            await self._send({"type": "enroll_started", "name": name,
                              "seconds": self.s.enroll_seconds})
        elif kind == "enroll_stop":
            await self._finish_enrollment()
        elif kind == "final":
            await self._classify(final=True)
        elif kind == "target":
            name = msg.get("name")
            self.target_name = name
            self.target = self.profiles.get(name)
            # Picking a voice implies clone-to-that-voice.
            set_fx = getattr(self.conv, "set_effect", None)
            if set_fx:
                set_fx("clone")
            await self._send({"type": "target_set", "name": name,
                              "found": self.target is not None})
            await self._send({"type": "effect_set", "name": "clone"})
        elif kind == "effect":
            name = str(msg.get("name") or "clean")
            set_fx = getattr(self.conv, "set_effect", None)
            if set_fx:
                set_fx(name)
            await self._send({"type": "effect_set", "name": name})
        elif kind == "ping":
            await self._send({"type": "pong"})
        elif kind == "stop":
            self._stop.set()
        else:
            raise ValueError(f"unknown message type: {kind!r}")

    # -- inference ---------------------------------------------------

    async def _infer_loop(self) -> None:
        while not self._stop.is_set():
            await asyncio.sleep(self.s.hop_sec)
            await self._classify(final=False)

    async def _classify(self, final: bool) -> None:
        window = self.ring.read_last(self.s.window_samples)
        if window.size < self.s.window_samples // 2:
            return
        rms = float(np.sqrt(np.mean(window ** 2)))
        t0 = time.perf_counter()
        identify = getattr(self.model, "identify", None)
        if rms < self.s.silence_rms:
            speaker, conf = None, 0.0
        elif identify is not None:
            label, conf = await asyncio.to_thread(
                identify, window, self.profiles.embeddings())
            speaker = label if conf >= self.s.conf_threshold else "unknown"
        else:
            speaker, conf = self.target_name, 0.0
        latency = (time.perf_counter() - t0) * 1000.0
        await self._send({
            "type": "result",
            "speaker": speaker,
            "confidence": round(conf, 3),
            "latency_ms": round(latency, 1),
            "rtf": round(latency / 1000.0 / self.s.window_sec, 3),
            "rms": round(rms, 4),
            "final": final,
        })

    async def _finish_enrollment(self) -> None:
        name, self.enrolling = self.enrolling, None
        if not name:
            return
        clip = self.ring.read_last(self.s.enroll_samples)
        rms = float(np.sqrt(np.mean(clip ** 2))) if clip.size else 0.0
        min_samples = min(self.s.enroll_samples, self.s.model_sr)
        if clip.size < min_samples or rms < self.s.silence_rms:
            await self._send({"type": "enrolled", "ok": False,
                              "name": name, "error": "clip too short/quiet"})
            return
        emb = await asyncio.to_thread(self.model.embed, clip)
        f0 = None
        analyze = getattr(self.model, "analyze_clip", None)
        if analyze:
            f0 = (await asyncio.to_thread(analyze, clip)).get("f0")
        self.profiles.add(name, emb, f0)
        await self._send({"type": "enrolled", "ok": True, "name": name,
                          "f0": f0, "profiles": self.profiles.names()})

    async def _send(self, payload: dict) -> None:
        try:
            await self.ws.send_json(payload)
        except Exception:
            self._stop.set()
