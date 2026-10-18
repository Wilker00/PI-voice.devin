"""PI Voice hybrid inference relay — runs on the Starbook (10.104.0.61:8081).

Accepts POST /convert (multipart: audio, ref[, threads]) and runs the tuned
audiocpp_cli MeanVC2 conversion, returning the converted WAV. The Pi's
vc_model backend reaches this through audiocpp_remote.sh (HTTP wrapper).

Why HTTP: the Starbook has no sshd and we can't sudo; this relay is one
process we fully control. Conversions are serialized (single model in RAM,
CPU-bound) — order from the Pi is naturally sequential anyway.
"""
import logging
import os
import shutil
import subprocess
import tempfile
import threading
import time

from fastapi import FastAPI, File, UploadFile
from fastapi.responses import PlainTextResponse, Response

DIR = "/home/robbie/audiocpp-x64"
MODEL = f"{DIR}/model/meanvc2-120ms-40ms-q4_k.gguf"
THREADS = 10                    # RTF ~1.09 measured on the 5800U
TIMEOUT_S = 180

app = FastAPI(title="PI Voice inference relay")
log = logging.getLogger("vc_relay")
logging.basicConfig(level=logging.INFO)
_lock = threading.Lock()


@app.get("/health")
def health():
    return {"ok": True, "model": os.path.basename(MODEL), "threads": THREADS}


@app.post("/convert")
def convert(audio: UploadFile = File(...), ref: UploadFile = File(...),
            threads: int = THREADS):
    with _lock:
        os.makedirs("/tmp/opencode", exist_ok=True)   # /tmp is wiped on reboot
        with tempfile.TemporaryDirectory(prefix="vc-relay-",
                                          dir="/tmp/opencode") as td:
            in_p, ref_p, out_p = (f"{td}/in.wav", f"{td}/ref.wav",
                                  f"{td}/out.wav")
            for path, up in ((in_p, audio), (ref_p, ref)):
                with open(path, "wb") as f:
                    shutil.copyfileobj(up.file, f)
            t0 = time.perf_counter()
            try:
                p = subprocess.run(
                    [f"{DIR}/audiocpp_cli", "--task", "vc",
                     "--family", "meanvc2", "--model", MODEL,
                     "--audio", in_p, "--voice-ref", ref_p,
                     "--out", out_p, "--backend", "cpu",
                     "--threads", str(threads), "--metrics"],
                    capture_output=True, text=True, timeout=TIMEOUT_S)
            except subprocess.TimeoutExpired:
                return PlainTextResponse("cli timeout", status_code=500)
            wall = time.perf_counter() - t0
            if p.returncode != 0 or not os.path.exists(out_p):
                log.error("cli rc=%s\n%s\n%s", p.returncode,
                          p.stdout[-800:], p.stderr[-800:])
                return PlainTextResponse(
                    f"cli failed rc={p.returncode}: {p.stderr[-400:]}",
                    status_code=500)
            log.info("convert ok: %.1fs wall, rc=0", wall)
            with open(out_p, "rb") as f:
                wav = f.read()
            return Response(content=wav, media_type="audio/wav")
