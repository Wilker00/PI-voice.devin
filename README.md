# PI Voice

Speaker-identification web app for Raspberry Pi 4: a phone browser captures
mic audio and streams PCM over WebSocket to the Pi, where a ring buffer feeds
a speaker model that names who's talking — with live latency/RTF/thermal
stats on the HUD.

## Run locally

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
./start.sh            # or: python -m uvicorn server.main:app --port 3000
```

Open `http://localhost:3000` — localhost is a secure context, so the mic
works without any browser flags.

## On the Pi

See [docs/SETUP_PI.md](docs/SETUP_PI.md) — `pi/setup.sh`, `start.sh`, the
nginx WebSocket site block, and the phone mic workarounds.

## Raspberry Pi connection

See [docs/SSH_CONNECTION.md](docs/SSH_CONNECTION.md) for the SSH connection details,
verification commands, and troubleshooting steps.
