"""Per-WebSocket client session: audio ingest, inference loop, enrollment."""
from __future__ import annotations

import asyncio
import json
import logging
import time

import numpy as np
from starlette.websockets import WebSocket

from .audio import (RingBuffer, StreamResampler, float32_to_pcm16,
                    pcm16_to_float32)
from .config import Settings
from .model import EFFECTS
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
        self._samples_in = 0               # total model-rate samples received
        self._ptt_mark: int | None = None  # _samples_in at push-to-talk press
        self.src_rate = settings.model_sr  # until client reports otherwise
        self._set_rate(self.src_rate)
        self.enrolling: str | None = None
        self.target_name: str | None = None
        self.target: dict | None = None   # {"emb": ndarray, "f0": float|None}
        # Prefer a stateful per-client stream when the model offers one.
        self.conv = (model.open_stream()
                     if hasattr(model, "open_stream") else model)
        self._stop = asyncio.Event()
        self._prepare_task: asyncio.Task | None = None
        self._transform_generation = 0
        self._last_character_effect = "robot"

    async def run(self) -> None:
        recv = asyncio.create_task(self._recv_loop())
        infer = asyncio.create_task(self._infer_loop())
        try:
            _, pending = await asyncio.wait(
                (recv, infer), return_when=asyncio.FIRST_COMPLETED)
            for task in pending:
                task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)
        finally:
            self._cancel_prepare()

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
        pcm = self._rs_in.process(pcm16_to_float32(data))
        self.ring.write(pcm)
        self._samples_in += pcm.size
        # Per-chunk conversion keeps the speaker loop low-latency.
        # While enrolling we buffer only — no playback to feed back.
        convert = getattr(self.conv, "convert", None)
        if convert is None or self.enrolling:
            return
        out = await asyncio.to_thread(convert, pcm, self.target)
        if out is None or out.size == 0:
            return
        out = self._rs_out.process(out)
        try:
            await self.ws.send_bytes(float32_to_pcm16(out))
        except Exception:
            self._stop.set()

    def _set_rate(self, rate: int) -> None:
        # Fresh stream state per "start" (also after a reconnect).
        self.src_rate = rate
        self._rs_in = StreamResampler(rate, self.s.model_sr)
        self._rs_out = StreamResampler(self.s.model_sr, rate)

    async def _on_control(self, msg: dict) -> None:
        kind = msg.get("type")
        if kind == "start":
            self._set_rate(int(msg.get("sampleRate") or self.s.model_sr))
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
        elif kind == "ptt_start":
            # Hold-to-identify: pause live results and classify exactly
            # the audio captured between press and release.
            self._ptt_mark = self._samples_in
        elif kind == "final":
            await self._classify_held()
        elif kind == "target":
            name = str(msg.get("name") or "").strip()
            await self._cancel_prepare_and_report()
            self.target_name = name
            self.target = self.profiles.get(name)
            # Keep audio immediate while a custom clone implementation warms.
            set_fx = getattr(self.conv, "set_effect", None)
            if set_fx:
                set_fx(self._last_character_effect)
            await self._send({"type": "target_set", "name": name,
                              "found": self.target is not None})
            await self._send({"type": "effect_set",
                              "name": self._last_character_effect})
            if self.target is None:
                await self._send({
                    "type": "transform_state", "phase": "fallback",
                    "target": name, "fallback": self._last_character_effect,
                    "reason": "error",
                })
                return
            await self._send({
                "type": "transform_state", "phase": "preparing",
                "target": name, "fallback": self._last_character_effect,
            })
            self._transform_generation += 1
            generation = self._transform_generation
            self._prepare_task = asyncio.create_task(
                self._prepare_clone(generation, name, self.target))
        elif kind == "effect":
            name = str(msg.get("name") or "clean")
            if name not in EFFECTS:
                raise ValueError(f"unknown effect: {name!r}")
            await self._cancel_prepare_and_report()
            set_fx = getattr(self.conv, "set_effect", None)
            if set_fx:
                set_fx(name)
            if name not in ("clean", "clone"):
                self._last_character_effect = name
            await self._send({"type": "effect_set", "name": name})
        elif kind == "ping":
            await self._send({"type": "pong"})
        elif kind == "stop":
            self._stop.set()
        else:
            raise ValueError(f"unknown message type: {kind!r}")

    async def _prepare_clone(self, generation: int, name: str,
                             target: dict) -> None:
        """Prepare a clone without pausing chunk-level DSP playback."""
        prepare = getattr(self.conv, "prepare_target", None)
        try:
            if prepare is not None:
                await asyncio.wait_for(
                    asyncio.to_thread(prepare, target),
                    timeout=self.s.clone_prepare_timeout_sec,
                )
            if generation != self._transform_generation or self._stop.is_set():
                return
            set_fx = getattr(self.conv, "set_effect", None)
            if set_fx:
                set_fx("clone")
            await self._send({"type": "effect_set", "name": "clone"})
            await self._send({
                "type": "transform_state", "phase": "ready",
                "target": name, "effect": "clone",
            })
        except asyncio.CancelledError:
            raise
        except asyncio.TimeoutError:
            if generation == self._transform_generation:
                await self._send({
                    "type": "transform_state", "phase": "fallback",
                    "target": name,
                    "fallback": self._last_character_effect,
                    "reason": "timeout",
                })
        except Exception:
            log.exception("clone preparation failed for %s", name)
            if generation == self._transform_generation:
                await self._send({
                    "type": "transform_state", "phase": "fallback",
                    "target": name,
                    "fallback": self._last_character_effect,
                    "reason": "error",
                })
        finally:
            if generation == self._transform_generation:
                self._prepare_task = None

    def _cancel_prepare(self) -> tuple[asyncio.Task | None, str | None]:
        task, target = self._prepare_task, self.target_name
        self._transform_generation += 1
        self._prepare_task = None
        if task and not task.done():
            task.cancel()
        return task, target

    async def _cancel_prepare_and_report(self) -> None:
        task, target = self._cancel_prepare()
        if task and not task.done() and target:
            await self._send({
                "type": "transform_state", "phase": "cancelled",
                "target": target,
            })

    # -- inference ---------------------------------------------------

    async def _infer_loop(self) -> None:
        while not self._stop.is_set():
            await asyncio.sleep(self.s.hop_sec)
            if self._ptt_mark is None:   # held: wait for the final result
                await self._classify(final=False)

    async def _classify_held(self) -> None:
        mark, self._ptt_mark = self._ptt_mark, None
        if mark is None:   # bare "final" (no press seen): use the live window
            await self._classify(final=True)
            return
        held = self._samples_in - mark
        # Very short taps borrow up to 1 s of pre-press audio; long holds
        # are capped by what the ring buffer still holds.
        n = min(max(held, self.s.model_sr), self.ring.capacity)
        await self._classify(final=True, n_samples=n,
                             held_sec=held / self.s.model_sr)

    async def _classify(self, final: bool, n_samples: int | None = None,
                        held_sec: float | None = None) -> None:
        n = n_samples or self.s.window_samples
        window = self.ring.read_last(n)
        if window.size < n // 2:
            if final:   # always answer a release, even with too little audio
                await self._send({"type": "result", "speaker": None,
                                  "confidence": 0.0, "final": True,
                                  "error": "not enough audio"})
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
            "rtf": round(latency * self.s.model_sr / 1000.0 / window.size, 3),
            "rms": round(rms, 4),
            "final": final,
            "window_sec": round(window.size / self.s.model_sr, 2),
            **({"held_sec": round(held_sec, 2)} if held_sec is not None else {}),
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
