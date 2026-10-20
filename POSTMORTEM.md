# POSTMORTEM — PI Voice (hackathon, Oct 18–19 2026)

Voice-changer mask on Raspberry Pi 4: phone as mic/speaker, Pi orchestrating,
zero-shot neural voice conversion (MeanVC2) offloaded to a LAN laptop.

## Outcome

Working end-to-end: enroll a teammate with a 10-second clip, tap their name,
speak, and the mask answers in their voice ~4–5 s later. Verified over
WebSocket through nginx (https + wss), with character DSP effects (robot,
megaphone, deep, chipmunk, F0-matched clone) running fully on-Pi as
instant-response/fallback modes.

```
Android Chrome ──wss──► nginx ──► FastAPI on Pi (:3000)
                                vc_model.py: VAD segmentation, profiles,
                                utterance buffering, playback, HUD
                                       │ per completed utterance (worker thread)
                                       ▼
                          audiocpp_remote.sh (HTTP) ──► vc_relay.py on laptop
                                                           audiocpp_cli
                                                           meanvc2 q4_k GGUF
```

Ops details: `docs/HYBRID.md`.

## The measured numbers (most durable artifact)

| Config | RTF | Notes |
|---|---|---|
| MeanVC2 Q4, Pi 4, 3 threads | 8.75 | temp 66.7 °C |
| MeanVC2 Q4, Pi 4, 4 threads | 7.42 | temp 74.5 °C — near throttle |
| MeanVC2 Q4, laptop 5800U, 6 threads | 1.45 | |
| — 8 threads | 1.42 | |
| — **10 threads** | **1.09** | the sweet spot |
| — 12 / 14 / 16 threads | 1.19 / 1.22 / 1.77 | more threads made it worse |

Lessons in the table: **pure-Pi neural VC is ~8× off real-time** (no tuning
closes that on a Cortex-A72), and **CPU inference is memory-bandwidth-bound** —
thread scaling is non-monotonic; sweep, don't guess. End-to-end per-utterance
cost including model reload + HTTP: RTF ~1.65 → ~5 s for a 3 s sentence.

## Technical lessons

1. **Right-size compute placement.** The Pi was excellent at everything
   except the neural math; the LAN laptop did that 7× faster. "Edge
   orchestrates, accelerator computes" preserved the demo — but the honest
   pitch carries the footnote.
2. **Zero-shot VC changed the product.** No training step → "clone an
   attendee in 10 seconds" — impossible with train-per-voice models on a
   hackathon clock.
3. **Never block the audio ingest path.** The worst UX bug: the recv loop
   awaited a ~5 s conversion while speech piled up behind it (compounding
   delay). Fix: worker thread + bounded drop-oldest queue (`CloneStream`).
   For anything live: newest-wins, not FIFO-accumulate. Proven fix:
   `convert()` latency 0.1 ms during in-flight conversion.
4. **Analyze the latency requirement before chasing real-time.** A mask is
   audience-facing: seconds of latency are theater, not failure. That made
   utterance-mode VAD (RMS + 350 ms hangover + 150 ms preroll + 350 ms/6 s
   min/max) the right design instead of a doomed streaming chase.
5. **Phone-as-thin-client is a great pattern for hardware-less SBC projects**
   — but budget real time for `getUserMedia` secure-context rules. The
   `chrome://flags` route failed on this phone; **self-signed cert with IP
   SAN** in nginx was the reliable fix. Continuous chunk streaming (silence
   included) is load-bearing: it's what lets results drain on the next call.
6. **Protocols that tolerate "no output" integrate invisibly.**
   `convert() → empty while buffering` dropped a blocking backend into the
   session loop without touching it; the `MODEL_MODULE` + mock-backend
   contract let the team build against a stub while the model landed
   (25 tests caught integration issues before any human testing).
7. **Cross-compile in Docker using the target's exact distro**
   (debian:trixie = the Pi's glibc) — zero ABI surprises.
   `GGML_NATIVE=OFF` + `-march=armv8-a+simd` is the Pi 4 recipe
   (Cortex-A72: NEON yes, no dotprod/i8mm/SVE).
8. **audio.cpp** (ggml, pure C++, ARM-optimized, MeanVC2 zero-shot VC in GGUF)
   is the discovery that made the plan feasible — remember it for any
   Pi audio-AI project.
9. **Thermals are a demo variable.** 67 °C near-idle in a case, 74.5 °C under
   sustained inference, 80 °C throttle. Airflow + the temp HUD in the UI.

## Process lessons

1. **The written handoff file was the highest-leverage artifact.** Verified
   environment facts, exact commands, gotchas, timeboxes, decision gates,
   and a report template → another agent executed a complex cross-compile
   autonomously and delivered in ~30 minutes.
2. **Pre-agree the fallback ladder before the benchmark.** When RTF came
   back RED, we pivoted to hybrid with zero debate — no sunk-cost flailing.
   The gate ("RTF > 2 → stop, hand back") existed before the first number.
3. **Read the code, not the README.** The repo README claimed
   speaker-identification scope; the code was actually our voice changer.
4. **Parallel agents work when the interface is a contract:** the
   MODEL_MODULE protocol, the repo as shared artifact, one handoff file.

## Incidents & footguns (all 5-minute fixes, all worth remembering)

- `pkill -f` matching its own command line — twice, local and remote
  variants. Fixes: `[u]vicorn` bracket trick; separate kill and start calls.
- systemd **respawned the port-3000 placeholder** (the "Devin's Inferno"
  starter app) minutes after we killed it → answer was `app.service`;
  `sudo systemctl disable --now app.service` — exactly what the starter's
  own header told us to do ("Replace it with your own").
- `start.sh` exec bit lost through transfer → "Permission denied" at launch.
- Missing `python-multipart` → FastAPI multipart routes 500.
- Lazy `FileResponse` reading a tempdir deleted before the response sent —
  return bytes, not paths, from inside `TemporaryDirectory`.
- No sshd on the accelerator laptop and no sudo → replaced the whole
  ssh-transport design with a 60-line HTTP relay. Constraints breed
  simpler designs.
- Config smell that shipped: a typo in `AUDIOCPP_MODEL` on the last
  restart — harmless only because the remote wrapper owns the real model
  path. Single-source configuration would have prevented it.

## Left on the table

1. **Resident model** (`audiocpp_server` holding MeanVC2 loaded) — would cut
   ~1.2 s off every utterance.
2. **MeanVC2 streaming mode** (the model is named "120ms/40ms" for a reason)
   — the real latency killer; never got its timebox.
3. **Rigorous quality listen-test** with real voices — quality validation
   stayed subjective throughout.
4. Two-phone concurrency (single-client assumption, lock-guarded),
   production auth hygiene, a proper systemd unit for the app itself.
