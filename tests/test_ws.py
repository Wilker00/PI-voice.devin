"""WS round-trip: start -> PCM frames -> result; enroll -> identify."""
import json

import numpy as np
import pytest
from fastapi.testclient import TestClient

from server.audio import float32_to_pcm16
from server.config import Settings, REPO_ROOT
from server.main import create_app


@pytest.fixture()
def app(tmp_path):
    s = Settings(
        host="127.0.0.1", port=0, model_sr=16000,
        buffer_seconds=4, window_sec=0.25, hop_sec=0.1,
        enroll_seconds=0.5, model_module=None,
        conf_threshold=0.3, silence_rms=0.001,
        profiles_path=tmp_path / "profiles.json",
        web_dir=REPO_ROOT / "web",
    )
    return create_app(s)


def tone(seconds, sr=16000, freq=220.0):
    t = np.arange(int(seconds * sr)) / sr
    return (0.3 * np.sin(2 * np.pi * freq * t)).astype(np.float32)


def push_pcm(ws, pcm, chunk=1600):
    for i in range(0, pcm.size, chunk):
        ws.send_bytes(float32_to_pcm16(pcm[i:i + chunk]))


def recv_until(ws, msg_type, tries=200):
    """Read server messages; skip converted-audio binary frames."""
    for _ in range(tries):
        m = ws.receive()
        if m.get("text"):
            payload = json.loads(m["text"])
            if payload["type"] == msg_type:
                return payload
    raise AssertionError(f"never saw {msg_type}")


def recv_bytes(ws, tries=200):
    for _ in range(tries):
        m = ws.receive()
        if m.get("bytes") is not None:
            return m["bytes"]
    raise AssertionError("never saw binary audio frame")


def test_ws_result_round_trip(app):
    client = TestClient(app)
    with client.websocket_connect("/ws") as ws:
        ws.send_text(json.dumps({"type": "start", "sampleRate": 16000}))
        assert recv_until(ws, "started")["model_sr"] == 16000
        push_pcm(ws, tone(1.0))
        # converted audio streams back as binary PCM16
        audio = recv_bytes(ws)
        assert len(audio) > 0 and len(audio) % 2 == 0
        result = recv_until(ws, "result")
        assert set(result) >= {"speaker", "confidence", "latency_ms", "rtf"}
        assert 0.0 <= result["confidence"] <= 1.0


def test_ws_enroll_then_identify(app):
    client = TestClient(app)
    with client.websocket_connect("/ws") as ws:
        ws.send_text(json.dumps({"type": "start", "sampleRate": 16000}))
        recv_until(ws, "started")
        push_pcm(ws, tone(0.6))
        ws.send_text(json.dumps({"type": "enroll_start", "name": "robbie"}))
        recv_until(ws, "enroll_started")
        ws.send_text(json.dumps({"type": "enroll_stop"}))
        enrolled = recv_until(ws, "enrolled")
        assert enrolled["ok"] and enrolled["name"] == "robbie"
        # selecting the enrolled voice as the conversion target
        ws.send_text(json.dumps({"type": "target", "name": "robbie"}))
        t = recv_until(ws, "target_set")
        assert t["name"] == "robbie" and t["found"] is True
        # keep streaming the same voice -> should eventually match "robbie"
        for _ in range(6):
            push_pcm(ws, tone(0.3))
            r = recv_until(ws, "result")
            if r.get("speaker") == "robbie":
                return
        pytest.fail("enrolled speaker never identified")


def test_api_endpoints(app):
    client = TestClient(app)
    assert client.get("/api/health").json()["ok"] is True
    assert "on_pi" in client.get("/api/system").json()
    assert client.get("/api/profiles").json() == {"profiles": []}
