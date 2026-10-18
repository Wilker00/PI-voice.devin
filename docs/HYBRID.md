# Hybrid deployment runbook (Pi 4 + Starbook)

## Architecture (measured 2026-10-18)

```
Android Chrome ──nginx:80──► FastAPI on Pi (:3000, 127.0.0.1)
                             vc_model.py: VAD segmentation, profiles,
                             utterance buffering, playback, HUD
                                    │
                                    │ per completed utterance
                                    ▼
                       ~/audiocpp/audiocpp_remote.sh (Pi)
                                    │ HTTP POST /convert (in.wav + ref.wav)
                                    ▼
                       vc_relay.py on Starbook (10.104.0.61:8081)
                                    │ audiocpp_cli --family meanvc2
                                    │ q4_k GGUF, --threads 10
                                    ▼
                              converted WAV back
```

## Measured numbers

| Config | RTF | Notes |
|---|---|---|
| MeanVC2 Q4 on Pi 4, 3 threads | 8.75 | RED gate — pure-Pi not viable |
| MeanVC2 Q4 on Pi 4, 4 threads | 7.42 | temp hit 74.5°C |
| MeanVC2 Q4 on Starbook 5800U, 10 threads | **1.09** | locked config |
| Hybrid per-utterance (incl. model load + HTTP) | ~1.65 | 3s sentence → ~5s response |

## Operations

**Starbook (10.104.0.61)** — relay must be running for conversions:

```bash
cd /home/robbie && nohup pi-voice/.venv/bin/python -m uvicorn \
  vc_relay:app --host 0.0.0.0 --port 8081 > /tmp/opencode/vc_relay.log 2>&1 &
curl -s http://127.0.0.1:8081/health   # sanity
```

Dependencies: `audiocpp-x64/` (prebuilt v0.9.2 x64 CPU build + GGUF in
`audiocpp-x64/model/`), pi-voice venv (fastapi, uvicorn, python-multipart).

**Pi (pi-03)** — app restart:

```bash
cd ~/PI-voice.devin
kill $(pgrep -f '[u]vicorn') 2>/dev/null; sleep 2
HOST=127.0.0.1 MODEL_MODULE=server.vc_model \
AUDIOCPP_BIN=$HOME/audiocpp/audiocpp_remote.sh \
AUDIOCPP_MODEL=$HOME/audiocpp/model/meanvc2/meanvc2-120ms-40ms-q4_k.gguf \
AUDIOCPP_THREADS=10 AUDIOCPP_TIMEOUT_S=150 \
VC_STATS_FILE=/tmp/vc_stats.json \
  nohup ./start-vc.sh > /tmp/pivoice.log 2>&1 &
```

If the wrapper's `AUDIOCPP_BIN` isn't executable, `start-vc.sh` falls back to
mock passthrough with a loud warning — always check the health/log after
restarting: `/api/health` and `grep 'VC_MODEL ready' /tmp/pivoice.log`.

**Live stats:** `cat /tmp/vc_stats.json` on the Pi (per-utterance RTF);
`tail /tmp/opencode/vc_relay.log` on the Starbook.

**Phone:** Android Chrome flag `chrome://flags/#unsafely-treat-insecure-origin-as-secure`
must include `http://10.104.0.55`. Then open `http://10.104.0.55`.

## Demo notes

- Keep sentences short (2–3 s) — response arrives ~4–5 s after you stop
  talking (the "mask is thinking" beat).
- Enroll a teammate: type name → Enroll → they talk 10 s → tap their name.
- Feedback: AEC toggle on the HUD (default on), 75% output cap, gate +
  limiter always on. If it sounds ducked, flip AEC off and keep volume low.
- If the Starbook sleeps or leaves the network, conversions fail silently
  per-utterance (logged); the app itself keeps running. Keep the laptop
  awake and on the hackathon WiFi.
