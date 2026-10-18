"""End-to-end check: simulate the phone against a live server.

Streams speech-like PCM to /ws exactly as the browser does, then reports
what comes back: converted-audio frames (+ their RMS — is anything audible?),
result messages, effect switching, and enrollment. Run with the server up:

    .venv/Scripts/python tests/e2e_check.py [port]
"""
import asyncio
import json
import sys

import numpy as np
import websockets

PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 3100
SR = 16000


def speech_like(seconds, sr=SR):
    """Voiced-ish signal: harmonics of ~180 Hz with a slow envelope."""
    t = np.arange(int(seconds * sr)) / sr
    env = 0.5 + 0.5 * np.sin(2 * np.pi * 3 * t)
    sig = (np.sin(2 * np.pi * 180 * t)
           + 0.5 * np.sin(2 * np.pi * 360 * t)
           + 0.25 * np.sin(2 * np.pi * 540 * t))
    return (0.2 * env * sig).astype(np.float32)


def pcm16(x):
    return (np.clip(x, -1, 1) * 32767).astype("<i2").tobytes()


async def main():
    results = {"binary_frames": 0, "binary_bytes": 0, "out_rms_max": 0.0,
               "results": [], "started": None, "effect_set": None,
               "enrolled": None}
    uri = f"ws://127.0.0.1:{PORT}/ws"
    async with websockets.connect(uri) as ws:
        await ws.send(json.dumps({"type": "start", "sampleRate": SR}))

        # stream ~1.5 s of speech-like audio in ~85 ms frames (phone-sized)
        audio = speech_like(1.5)
        frame = int(0.085 * SR)

        async def pump():
            for i in range(0, len(audio), frame):
                await ws.send(pcm16(audio[i:i + frame]))
                await asyncio.sleep(0.02)      # faster than realtime: ~4x
            # let the inference hop fire, then exercise controls
            await asyncio.sleep(0.8)
            await ws.send(json.dumps({"type": "effect", "name": "deep"}))
            await asyncio.sleep(0.3)
            await ws.send(json.dumps({"type": "effect", "name": "clean"}))
            await asyncio.sleep(0.3)
            await ws.send(json.dumps(
                {"type": "enroll_start", "name": "e2e-bot"}))
            for i in range(0, len(audio), frame):     # enroll clip
                await ws.send(pcm16(audio[i:i + frame]))
                await asyncio.sleep(0.005)
            await ws.send(json.dumps({"type": "enroll_stop"}))
            await asyncio.sleep(0.6)

        done = asyncio.Event()

        async def drain():
            try:
                while not done.is_set():
                    try:
                        msg = await asyncio.wait_for(ws.recv(), timeout=0.5)
                    except asyncio.TimeoutError:
                        continue
                    if isinstance(msg, bytes):
                        results["binary_frames"] += 1
                        results["binary_bytes"] += len(msg)
                        x = np.frombuffer(msg, "<i2").astype(np.float32) / 32768
                        results["out_rms_max"] = max(
                            results["out_rms_max"], float(np.sqrt((x ** 2).mean())))
                    else:
                        m = json.loads(msg)
                        t = m.get("type")
                        if t == "result":
                            results["results"].append(m)
                        elif t in ("started", "effect_set", "enrolled",
                                   "enroll_started", "target_set"):
                            results[t] = m
            except websockets.ConnectionClosed:
                pass

        p = asyncio.create_task(pump())
        d = asyncio.create_task(drain())
        await p
        await asyncio.sleep(1.0)   # catch trailing responses
        done.set()
        await asyncio.wait_for(d, timeout=5)

    r = results
    ok = True
    def check(label, cond, extra=""):
        nonlocal ok
        ok &= bool(cond)
        print(f"  [{'PASS' if cond else 'FAIL'}] {label} {extra}")

    print("\n=== E2E report ===")
    check("ws start handshake", r["started"] and r["started"]["type"] == "started")
    check("converted audio returned", r["binary_frames"] > 0,
          f"({r['binary_frames']} frames, {r['binary_bytes']} bytes)")
    check("returned audio is audible", r["out_rms_max"] > 0.01,
          f"(max output RMS {r['out_rms_max']:.4f})")
    check("inference results flowing", len(r["results"]) >= 3,
          f"({len(r['results'])} messages)")
    if r["results"]:
        last = r["results"][-1]
        print(f"      last result: speaker={last['speaker']} "
          f"conf={last['confidence']} lat={last['latency_ms']}ms "
          f"rtf={last['rtf']}")
    check("effect_set acknowledged", r["effect_set"] is not None,
          f"({r['effect_set']})")
    check("enrollment worked",
          r["enrolled"] and r["enrolled"].get("ok"),
          f"({r['enrolled']})")
    print("=== " + ("ALL GOOD" if ok else "ISSUES FOUND") + " ===")
    return ok


asyncio.run(main())
