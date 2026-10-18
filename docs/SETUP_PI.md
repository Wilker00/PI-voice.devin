# Running PI Voice on the Raspberry Pi

The app is a single Python process (FastAPI + uvicorn) that serves the web
frontend, the WebSocket audio channel, and the speaker model. It runs plain
HTTP behind nginx.

## 1. Get the code on the Pi

Per `docs/SSH_CONNECTION.md` — SSH in, then clone with the Pi's deploy key:

```bash
ssh hacker3@pi-03.local
git clone https://github.com/Wilker00/PI-voice.devin.git
cd PI-voice.devin
```

## 2. Install

```bash
./pi/setup.sh        # creates .venv and installs requirements.txt
```

## 3. Run

```bash
./start.sh           # binds 0.0.0.0:${PORT:-3000}
# or on another port:
PORT=3000 ./start.sh
```

`start.sh` uses `.venv/bin/python` when present, else `python3`.

## 4. nginx site

The app expects nginx to forward :80 to it. WebSockets **require** the
`Upgrade`/`Connection` headers — without them `/ws` fails and no audio flows:

```nginx
server {
    listen 80 default_server;
    server_name _;

    location / {
        proxy_pass http://127.0.0.1:3000;
        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "upgrade";
        proxy_set_header Host $host;
        proxy_read_timeout 3600s;
    }
}
```

Drop it in `/etc/nginx/sites-available/pi-voice`, symlink into
`sites-enabled`, then `sudo nginx -t && sudo systemctl reload nginx`.

## 5. Phone access + the mic/secure-context gotcha

Pi on the team network: `10.104.0.55` (robbie, 2026-10-18) — prefer
`http://10.104.0.55` if `pi-03.local` doesn't resolve.

`getUserMedia` only works in a **secure context**. Plain HTTP is not one,
so pick one fix on the demo phone:

- **Android Chrome (fastest):** open
  `chrome://flags/#unsafely-treat-insecure-origin-as-secure`, add
  `http://10.104.0.55` and `http://pi-03.local`, relaunch Chrome.
- **Laptop dev:** `http://localhost:3000` is a secure context — mic works
  with no changes.
- **Alternative:** terminate TLS in nginx with a self-signed cert and accept
  the warning once (app itself stays plain HTTP).

## Feedback control (mic + speaker on the same phone)

The phone plays the converted voice while its mic is still live, so the
app ships the team's feedback plan built in:

- **AEC toggle on the HUD, default ON** — `echoCancellation:true` gives
  no-howl operation; the phone hears its own speaker as the far-end
  reference. If M4 tuning sounds ducked/gated, flip it off and the gate +
  limiter still protect the loop. `noiseSuppression`/`autoGainControl`
  stay off permanently (they mangle voice identity).
- **Output capped at 75% gain**, always-on per-frame noise gate (~-48 dBFS)
  and a DynamicsCompressor limiter as last-stage safety.
- **Demo choreography:** phone mic toward the mouth, speaker toward the
  audience, lowest theatrically-acceptable volume; the mask's foam doubles
  as acoustic separation.
- **"Not hearing anything?" checklist:** HUD `SOCKET` = ON and `AUDIO RX`
  counter climbing = audio IS reaching the phone → the problem is speaker
  routing. On Android Chrome, `echoCancellation:true` can switch to
  call-mode audio and play through the **earpiece** at low volume instead
  of the loudspeaker — flip the AEC toggle off. Also check media volume
  (not ringer volume) and that no Bluetooth device stole the output.

## 6. Features / endpoints

| What | Where |
| --- | --- |
| Web UI | `GET /` |
| Audio + control socket | `WS /ws` |
| Health | `GET /api/health` |
| Thermal/throttle stats (`vcgencmd`) | `GET /api/system` — shown as the on-page badge, polled every 5 s |
| Enrolled voices | `GET/DELETE /api/profiles[/{name}]` |

Baseline: `vcgencmd measure_temp` read **43.3 °C** at idle on pi-03
(2026-10-18) — handy reference for the "look, we track thermals" overlay.

Enroll flow: type a name → **Enroll (10s)** → talk → embedding is computed
on the Pi and stored in `server/data/profiles.json`. Tap an enrolled name
to set the conversion target; live identification names who's speaking,
with latency + RTF in the HUD. Converted audio streams back over the same
socket as binary PCM and plays through the phone speaker.

## 7. Plugging in the real model

Set `MODEL_MODULE` to a module exposing `embed(pcm)`,
`convert(pcm, target_emb)`, and optionally `identify(pcm, profiles)`
(or a `load()` returning such an object):

```bash
MODEL_MODULE=pi_voice_model ./start.sh
```

`pcm` is a float32 numpy array at `MODEL_SR` (16 kHz mono); conversion
runs per ~85 ms chunk so keep it streaming-friendly. A model can also
expose `open_stream()` returning a **stateful** per-client converter
with `convert(pcm, target)` + `set_effect(name)` — `target` is the
selected profile `{"emb", "f0"}`.

## Built-in voice changer

Until the real model lands, `VoiceChangerModel` does actual conversion:

| Button | What it does |
| --- | --- |
| Clean | Passthrough |
| Deep | Pitch shift −5 st (delay-line dual-reader shifter) |
| Chipmunk | Pitch shift +6 st |
| Robot | 30 Hz ring-mod (Dalek) |
| Mega | Soft-clip + band-smoothing loudspeaker coloration |
| *(tap enrolled name)* | **Clone** — pitch-matches your live F0 toward the target's F0 measured at enrollment (EMA-smoothed, clamped 0.6–1.7×) |

Clone mode uses the enrolled clip's autocorrelation F0 — it shifts pitch
toward the teammate's register, not their full timbre; that's what the
real model is for. All effects are stateful per-client via `open_stream()`.

## Config env vars

`HOST` `PORT` `MODEL_SR` `BUFFER_SECONDS` `WINDOW_SEC` `HOP_SEC`
`ENROLL_SECONDS` `CONF_THRESHOLD` `SILENCE_RMS` `PROFILES_PATH` `WEB_DIR`
`MODEL_MODULE`
