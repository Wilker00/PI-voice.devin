"""WS round-trip: start -> PCM frames -> result; enroll -> identify."""
import json
import time

import numpy as np
import pytest
from fastapi.testclient import TestClient

from server.audio import float32_to_pcm16
from server.config import Settings, REPO_ROOT
from server.main import create_app
from server.profiles import ProfileStore


@pytest.fixture()
def app(tmp_path):
    s = Settings(
        host="127.0.0.1", port=0, model_sr=16000,
        buffer_seconds=4, window_sec=0.25, hop_sec=0.1,
        enroll_seconds=0.5, model_module=None,
        conf_threshold=0.3, silence_rms=0.001,
        clone_prepare_timeout_sec=0.25,
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
        # clone path: current effect keeps playing while preparing,
        # then the transform flips to clone + ready once prepared
        seen = {"fx": [], "phases": []}
        for _ in range(40):
            m = ws.receive()
            if m.get("bytes") is not None or not m.get("text"):
                continue
            p = json.loads(m["text"])
            if p["type"] == "effect_set":
                seen["fx"].append(p["name"])
            elif p["type"] == "transform_state":
                seen["phases"].append(p["phase"])
            if "ready" in seen["phases"]:
                break
        assert "preparing" in seen["phases"]
        assert "ready" in seen["phases"]
        assert seen["fx"] == ["clone"]
        # manual effect switch works too
        ws.send_text(json.dumps({"type": "effect", "name": "deep"}))
        assert recv_until(ws, "effect_set")["name"] == "deep"
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


def test_missing_clone_target_keeps_current_effect(app):
    client = TestClient(app)
    with client.websocket_connect("/ws") as ws:
        ws.send_text(json.dumps({"type": "target", "name": "missing"}))
        assert recv_until(ws, "target_set")["found"] is False
        fallback = recv_until(ws, "transform_state")
        assert fallback["phase"] == "fallback"
        assert fallback["fallback"] == "clean"
        assert fallback["reason"] == "error"


def test_ptt_classifies_exactly_the_held_audio(app):
    client = TestClient(app)
    with client.websocket_connect("/ws") as ws:
        ws.send_text(json.dumps({"type": "start", "sampleRate": 16000}))
        recv_until(ws, "started")
        push_pcm(ws, tone(1.0))
        ws.send_text(json.dumps({"type": "ptt_start"}))
        push_pcm(ws, tone(2.0))
        ws.send_text(json.dumps({"type": "final"}))
        r = recv_until(ws, "result")
        while not r["final"]:   # live results from before the press
            r = recv_until(ws, "result")
        assert r["held_sec"] == 2.0 and r["window_sec"] == 2.0


def test_ptt_short_tap_borrows_one_second(app):
    client = TestClient(app)
    with client.websocket_connect("/ws") as ws:
        ws.send_text(json.dumps({"type": "start", "sampleRate": 16000}))
        recv_until(ws, "started")
        push_pcm(ws, tone(1.5))
        ws.send_text(json.dumps({"type": "ptt_start"}))
        push_pcm(ws, tone(0.2))
        ws.send_text(json.dumps({"type": "final"}))
        r = recv_until(ws, "result")
        while not r["final"]:
            r = recv_until(ws, "result")
        assert r["held_sec"] == 0.2 and r["window_sec"] == 1.0


def test_ptt_release_without_audio_still_answers(app):
    client = TestClient(app)
    with client.websocket_connect("/ws") as ws:
        ws.send_text(json.dumps({"type": "start", "sampleRate": 16000}))
        recv_until(ws, "started")
        ws.send_text(json.dumps({"type": "ptt_start"}))
        ws.send_text(json.dumps({"type": "final"}))
        r = recv_until(ws, "result")
        assert r["final"] and r["error"] == "not enough audio"


class PreparingStream:
    def __init__(self, delay=0.0, failure=False):
        self.delay = delay
        self.failure = failure
        self.effects = []
        self.convert_calls = 0

    def set_effect(self, name):
        self.effects.append(name)

    def prepare_target(self, target):
        time.sleep(self.delay)
        if self.failure:
            raise RuntimeError("clone warmup failed")

    def convert(self, pcm, target):
        self.convert_calls += 1
        return pcm


class PreparingModel:
    def __init__(self, stream):
        self.stream = stream

    def open_stream(self):
        return self.stream

    def embed(self, pcm):
        return np.ones(12, dtype=np.float32)

    def identify(self, pcm, profiles):
        return "alex", 0.95


def app_with_preparer(monkeypatch, tmp_path, stream, timeout=0.25,
                      profiles=("alex",)):
    import server.main as main_module

    path = tmp_path / "prepared-profiles.json"
    store = ProfileStore(path)
    for name in profiles:
        store.add(name, np.ones(12, dtype=np.float32), 180.0)
    model = PreparingModel(stream)
    monkeypatch.setattr(main_module, "load_model", lambda: model)
    settings = Settings(
        host="127.0.0.1", port=0, model_sr=16000,
        buffer_seconds=4, window_sec=0.25, hop_sec=0.1,
        enroll_seconds=0.5, model_module=None,
        conf_threshold=0.3, silence_rms=0.001,
        profiles_path=path, web_dir=REPO_ROOT / "web",
        clone_prepare_timeout_sec=timeout,
    )
    return create_app(settings)


def test_clone_preparation_keeps_chunk_dsp_live(monkeypatch, tmp_path):
    stream = PreparingStream(delay=0.25)
    client = TestClient(app_with_preparer(
        monkeypatch, tmp_path, stream, timeout=0.6))
    with client.websocket_connect("/ws") as ws:
        ws.send_text(json.dumps({"type": "target", "name": "alex"}))
        assert recv_until(ws, "target_set")["found"] is True
        assert recv_until(ws, "transform_state")["phase"] == "preparing"
        ws.send_bytes(float32_to_pcm16(tone(0.1)))
        assert recv_bytes(ws)
        assert stream.convert_calls == 0
        assert recv_until(ws, "effect_set")["name"] == "clone"
        assert recv_until(ws, "transform_state")["phase"] == "ready"


@pytest.mark.parametrize("failure,reason", [(False, "timeout"), (True, "error")])
def test_clone_preparation_falls_back(monkeypatch, tmp_path, failure, reason):
    stream = PreparingStream(delay=0.08 if not failure else 0, failure=failure)
    timeout = 0.01 if not failure else 0.25
    client = TestClient(app_with_preparer(
        monkeypatch, tmp_path, stream, timeout=timeout))
    with client.websocket_connect("/ws") as ws:
        ws.send_text(json.dumps({"type": "target", "name": "alex"}))
        assert recv_until(ws, "transform_state")["phase"] == "preparing"
        fallback = recv_until(ws, "transform_state")
        assert fallback["phase"] == "fallback"
        assert fallback["fallback"] == "clean"
        assert fallback["reason"] == reason
        assert "clone" not in stream.effects


def test_new_target_cancels_obsolete_clone(monkeypatch, tmp_path):
    stream = PreparingStream(delay=0.25)
    client = TestClient(app_with_preparer(
        monkeypatch, tmp_path, stream, timeout=0.6,
        profiles=("alex", "sam")))
    with client.websocket_connect("/ws") as ws:
        ws.send_text(json.dumps({"type": "target", "name": "alex"}))
        assert recv_until(ws, "transform_state")["phase"] == "preparing"
        ws.send_text(json.dumps({"type": "target", "name": "sam"}))
        cancelled = recv_until(ws, "transform_state")
        assert cancelled["phase"] == "cancelled"
        assert cancelled["target"] == "alex"
        assert recv_until(ws, "target_set")["name"] == "sam"
        assert recv_until(ws, "transform_state")["phase"] == "preparing"
        assert recv_until(ws, "effect_set")["name"] == "clone"
        ready = recv_until(ws, "transform_state")
        assert ready["phase"] == "ready" and ready["target"] == "sam"


def test_target_switch_preserves_interim_effect(monkeypatch, tmp_path):
    stream = PreparingStream(delay=0.25)
    client = TestClient(app_with_preparer(
        monkeypatch, tmp_path, stream, timeout=0.6,
        profiles=("alex", "sam")))
    with client.websocket_connect("/ws") as ws:
        ws.send_text(json.dumps({"type": "effect", "name": "deep"}))
        assert recv_until(ws, "effect_set")["name"] == "deep"
        # on a character effect: it stays live while clone warms
        ws.send_text(json.dumps({"type": "target", "name": "alex"}))
        assert recv_until(ws, "target_set")["found"] is True
        prep = recv_until(ws, "transform_state")
        assert prep["phase"] == "preparing" and prep["fallback"] == "deep"
        assert recv_until(ws, "effect_set")["name"] == "clone"
        assert recv_until(ws, "transform_state")["phase"] == "ready"
        # now cloned to alex: picking sam can't keep the stale clone live,
        # so it drops to the last character effect while preparing
        ws.send_text(json.dumps({"type": "target", "name": "sam"}))
        assert recv_until(ws, "effect_set")["name"] == "deep"
        assert recv_until(ws, "target_set")["name"] == "sam"
        assert recv_until(ws, "transform_state")["phase"] == "preparing"
        assert recv_until(ws, "effect_set")["name"] == "clone"


def test_manual_effect_cancels_clone_handoff(monkeypatch, tmp_path):
    stream = PreparingStream(delay=0.25)
    client = TestClient(app_with_preparer(
        monkeypatch, tmp_path, stream, timeout=0.6))
    with client.websocket_connect("/ws") as ws:
        ws.send_text(json.dumps({"type": "target", "name": "alex"}))
        assert recv_until(ws, "transform_state")["phase"] == "preparing"
        ws.send_text(json.dumps({"type": "effect", "name": "deep"}))
        assert recv_until(ws, "transform_state")["phase"] == "cancelled"
        assert recv_until(ws, "effect_set")["name"] == "deep"
        time.sleep(0.3)
        ws.send_text(json.dumps({"type": "ping"}))
        assert recv_until(ws, "pong")["type"] == "pong"
        assert stream.effects[-1] == "deep"


def test_disconnect_cancels_clone_handoff(monkeypatch, tmp_path):
    stream = PreparingStream(delay=0.25)
    client = TestClient(app_with_preparer(
        monkeypatch, tmp_path, stream, timeout=0.6))
    with client.websocket_connect("/ws") as ws:
        ws.send_text(json.dumps({"type": "target", "name": "alex"}))
        assert recv_until(ws, "transform_state")["phase"] == "preparing"
    time.sleep(0.3)
    assert "clone" not in stream.effects


def test_identify_failure_does_not_kill_session(monkeypatch, tmp_path):
    class BadIdentify:
        def open_stream(self):
            return PreparingStream()

        def embed(self, pcm):
            return np.ones(11, dtype=np.float32)

        def identify(self, pcm, profiles):
            raise RuntimeError("boom")

    import server.main as main_module
    monkeypatch.setattr(main_module, "load_model", lambda: BadIdentify())
    settings = Settings(
        host="127.0.0.1", port=0, model_sr=16000,
        buffer_seconds=4, window_sec=0.25, hop_sec=0.1,
        enroll_seconds=0.5, model_module=None,
        conf_threshold=0.3, silence_rms=0.001,
        clone_prepare_timeout_sec=0.25,
        profiles_path=tmp_path / "profiles.json",
        web_dir=REPO_ROOT / "web",
    )
    client = TestClient(create_app(settings))
    with client.websocket_connect("/ws") as ws:
        ws.send_text(json.dumps({"type": "start", "sampleRate": 16000}))
        recv_until(ws, "started")
        push_pcm(ws, tone(1.0))
        for _ in range(2):   # infer loop survives and keeps reporting
            r = recv_until(ws, "result")
            assert r["speaker"] is None
            assert r["error"].startswith("identify:")
        ws.send_text(json.dumps({"type": "ping"}))
        assert recv_until(ws, "pong")["type"] == "pong"
