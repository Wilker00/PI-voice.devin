"""vc_model tests: VAD segmentation, utterance-mode protocol, WAV I/O,
and an end-to-end run through the real FastAPI session with the mock
backend. The real audiocpp backend is skipped unless AUDIOCPP_BIN is set.
"""
import json
import os
import subprocess
import sys

import numpy as np
import pytest
from fastapi.testclient import TestClient

from server.audio import float32_to_pcm16
from server.config import REPO_ROOT, Settings
from server.main import create_app
from server.profiles import ProfileStore
from server.vc_model import (AudiocppCliBackend, MockBackend, Segmenter,
                             VCModel, read_wav, write_wav)

SR = 16000


# -- synthetic audio ---------------------------------------------------------

def speech(seconds, sr=SR, freq=190.0, amp=0.25):
    t = np.arange(int(seconds * sr)) / sr
    x = (amp * (np.sin(2 * np.pi * freq * t)
                + 0.5 * np.sin(2 * np.pi * 380 * t)
                + 0.25 * np.sin(2 * np.pi * 760 * t))) / 1.75
    return x.astype(np.float32)


def sil(seconds, sr=SR):
    return np.zeros(int(seconds * sr), np.float32)


def chunks(pcm, chunk=1365):        # ~85 ms, like the phone's worklet frames
    for i in range(0, pcm.size, chunk):
        yield pcm[i:i + chunk]


def feed_all(seg, pcm, chunk=1365):
    outs = []
    for ch in chunks(pcm, chunk):
        outs += seg.feed(ch)
    return outs


# -- Segmenter ----------------------------------------------------------------

def test_segmenter_single_utterance():
    seg = Segmenter(SR)
    outs = feed_all(seg, np.concatenate([sil(0.3), speech(0.8), sil(0.6)]))
    assert len(outs) == 1
    dur = outs[0].size / SR
    assert 0.9 <= dur <= 1.5
    assert float(np.max(np.abs(outs[0]))) > 0.05    # contains the speech


def test_segmenter_two_utterances():
    seg = Segmenter(SR)
    pcm = np.concatenate([speech(0.5), sil(0.7), speech(0.5), sil(0.7)])
    outs = feed_all(seg, pcm)
    assert len(outs) == 2


def test_segmenter_all_silence_emits_nothing_and_stays_bounded():
    seg = Segmenter(SR)
    outs = feed_all(seg, sil(5.0))
    assert outs == []
    assert seg._total == 0 and seg._preroll_buf.size <= seg.preroll


def test_segmenter_max_cap_splits_long_monologue():
    seg = Segmenter(SR, max_utt_s=2.0, preroll_ms=100)
    outs = feed_all(seg, speech(10.0))
    assert len(outs) >= 4
    for u in outs:
        assert u.size / SR <= 2.0 + 0.25     # cap + preroll + one frame


def test_segmenter_ignores_short_blip():
    seg = Segmenter(SR)
    outs = feed_all(seg, np.concatenate([sil(0.3), speech(0.1), sil(0.6)]))
    assert outs == []


# -- WAV I/O -------------------------------------------------------------------

def test_wav_roundtrip():
    import tempfile
    x = speech(0.5)
    with tempfile.TemporaryDirectory() as td:
        p = os.path.join(td, "t.wav")
        write_wav(p, x, SR)
        y, sr = read_wav(p)
    assert sr == SR
    assert y.size == x.size
    assert np.max(np.abs(y - x)) <= 1.0 / 32767.0


# -- VCModel protocol (mock backend) -------------------------------------------

def make_model(min_ref_s=0.3, **seg_kw):
    return VCModel(SR, backend=MockBackend(), min_ref_s=min_ref_s, **seg_kw)


def test_convert_buffers_then_emits_one_block():
    m = make_model()
    ref = m.embed(speech(1.0))
    assert ref.size >= SR                      # embed returns the raw clip
    outs, empty_calls = [], 0
    for ch in chunks(np.concatenate([sil(0.2), speech(0.8), sil(0.6)])):
        out = m.convert(ch, ref)
        if out.size:
            outs.append(out)
        else:
            empty_calls += 1
    assert len(outs) == 1, "expected exactly one converted block"
    assert empty_calls >= 10, "most chunk calls must return empty (buffering)"
    dur = outs[0].size / SR
    assert 0.9 <= dur <= 1.5
    assert float(np.max(np.abs(outs[0]))) > 0.05
    assert m.last_stats.get("rtf") is not None and m.last_stats["rtf"] >= 0


def test_convert_passthrough_without_target():
    m = make_model()
    x = speech(0.2)
    y = m.convert(x, None)
    assert y.size == x.size
    assert np.array_equal(y, x)
    y2 = m.convert(x, np.zeros(100, np.float32))   # ref too short -> raw
    assert np.array_equal(y2, x)


def test_embed_survives_profile_store_json_roundtrip():
    m = make_model()
    emb = m.embed(speech(0.6))
    stored = json.dumps(emb.tolist())               # what ProfileStore does
    back = np.asarray(json.loads(stored), np.float32)
    outs = []
    for ch in chunks(np.concatenate([speech(0.5), sil(0.7)])):
        out = m.convert(ch, back)
        if out.size:
            outs.append(out)
    assert len(outs) == 1


def test_identify_picks_right_profile():
    m = make_model()
    profiles = {
        "alice": m.embed(speech(1.0, freq=190)),
        "bob": m.embed(speech(1.0, freq=700)),
    }
    label, conf = m.identify(speech(0.5, freq=190), profiles)
    assert label == "alice"
    assert 0.0 <= conf <= 1.0
    label2, _ = m.identify(speech(0.5, freq=700), profiles)
    assert label2 == "bob"


def test_clone_stream_drops_stale_work_on_effect_change():
    import time

    class SlowBackend:
        name = "slow"

        def convert(self, utt, ref):
            time.sleep(0.2)
            return utt * 0.5

    m = VCModel(SR, backend=SlowBackend(), min_ref_s=0.3)
    st = m.open_stream()
    ref = m.embed(speech(0.6))
    for ch in chunks(np.concatenate([speech(0.5), sil(0.6)])):
        st.convert(ch, ref)          # one utterance reaches the worker
    st.set_effect("robot")           # leaving clone kills queued + in-flight
    st.set_effect("clone")
    time.sleep(0.3)                  # the stale conversion finishes anyway
    for ch in chunks(sil(0.3)):
        assert st.convert(ch, ref).size == 0
    st.close()


def test_clone_stream_worker_exits_on_close():
    m = make_model()
    st = m.open_stream()
    st.close()
    st._thread.join(timeout=2)
    assert not st._thread.is_alive()


def test_enroll_clip_tool(tmp_path):
    wav = tmp_path / "clip.wav"
    write_wav(str(wav), speech(1.0, freq=190), SR)
    prof = tmp_path / "profiles.json"
    env = {**os.environ, "PROFILES_PATH": str(prof),
           "MODEL_MODULE": "server.vc_model", "VC_BACKEND": "mock"}
    r = subprocess.run(
        [sys.executable, str(REPO_ROOT / "tools" / "enroll_clip.py"),
         "clipper", str(wav)],
        capture_output=True, text=True, env=env, cwd=REPO_ROOT)
    assert r.returncode == 0, r.stderr
    emb = ProfileStore(prof).get("clipper")["emb"]
    assert emb.size == SR          # vc_model stores the whole clip as ref


def test_stats_sidecar_written(tmp_path):
    f = tmp_path / "stats.json"
    m = VCModel(SR, backend=MockBackend(), min_ref_s=0.3, stats_file=str(f))
    ref = m.embed(speech(0.5))
    for ch in chunks(np.concatenate([speech(0.5), sil(0.7)])):
        m.convert(ch, ref)
    assert f.exists()
    assert json.loads(f.read_text())["backend"] == "mock"


# -- Real backend: only when a binary is configured ------------------------------

@pytest.mark.skipif(not os.environ.get("AUDIOCPP_BIN"),
                    reason="no audiocpp binary configured")
def test_audiocpp_backend_smoke():
    m = VCModel(SR,
                backend=AudiocppCliBackend(
                    bin=os.environ["AUDIOCPP_BIN"],
                    model=os.environ.get("AUDIOCPP_MODEL", ""),
                    sr=SR, threads=3),
                min_ref_s=0.3)
    ref = m.embed(speech(1.0))
    out = m.backend.convert(speech(1.0), ref)
    assert out is not None and out.size > 0


# -- End-to-end through the real WebSocket session -------------------------------

def recv_until(ws, msg_type, tries=300):
    for _ in range(tries):
        m = ws.receive()
        if m.get("text"):
            payload = json.loads(m["text"])
            if payload["type"] == msg_type:
                return payload
    raise AssertionError(f"never saw {msg_type}")


def drain_binary(ws, need_bytes, tries=400):
    """Collect binary frames until `need_bytes` total, skipping JSON."""
    frames = []
    for _ in range(tries):
        m = ws.receive()
        if m.get("bytes") is not None:
            frames.append(m["bytes"])
            if sum(len(f) for f in frames) >= need_bytes:
                break
    return frames


def test_vc_model_end_to_end_ws(monkeypatch, tmp_path):
    monkeypatch.setenv("MODEL_MODULE", "server.vc_model")
    monkeypatch.setenv("VC_BACKEND", "mock")
    monkeypatch.setenv("VC_MIN_REF_S", "0.3")
    monkeypatch.setenv("VC_STATS_FILE", str(tmp_path / "stats.json"))

    s = Settings(
        host="127.0.0.1", port=0, model_sr=16000,
        buffer_seconds=4, window_sec=0.25, hop_sec=0.1,
        enroll_seconds=0.5, model_module="server.vc_model",
        conf_threshold=0.3, silence_rms=0.001,
        profiles_path=tmp_path / "profiles.json",
        web_dir=REPO_ROOT / "web",
    )
    app = create_app(s)
    client = TestClient(app)
    with client.websocket_connect("/ws") as ws:
        ws.send_text(json.dumps({"type": "start", "sampleRate": 16000}))
        assert recv_until(ws, "started")["model_sr"] == 16000

        # enroll "robbie" from buffered audio (clip comes off the ring)
        for i in range(0, speech(0.6).size, 1600):
            ws.send_bytes(float32_to_pcm16(speech(0.6)[i:i + 1600]))
        ws.send_text(json.dumps({"type": "enroll_start", "name": "robbie"}))
        recv_until(ws, "enroll_started")
        ws.send_text(json.dumps({"type": "enroll_stop"}))
        enrolled = recv_until(ws, "enrolled")
        assert enrolled["ok"] is True

        ws.send_text(json.dumps({"type": "target", "name": "robbie"}))
        assert recv_until(ws, "target_set")["found"] is True

        # stream one utterance + long trailing silence; expect buffering
        # then exactly ONE big converted block. The phone never stops
        # streaming frames, and CloneStream drains converted output on
        # later chunks — keep the silence long enough to flush the worker.
        utt = np.concatenate([speech(0.8), sil(2.0)])
        for i in range(0, utt.size, 1600):
            ws.send_bytes(float32_to_pcm16(utt[i:i + 1600]))

        frames = drain_binary(ws, need_bytes=int(1.0 * SR * 2))
        small = [f for f in frames if len(f) <= 4000]
        big = [f for f in frames if len(f) > 4000]
        # 6 passthrough echoes from the pre-enroll push (target was unset),
        # then exactly one converted utterance (~0.8-1.3 s) after target set
        assert len(big) == 1, f"unexpected frames: {[len(f) for f in frames]}"
        assert len(small) <= 6, f"buffering leaked: {[len(f) for f in small]}"
        assert int(0.7 * SR * 2) <= len(big[0]) <= int(1.6 * SR * 2)

    assert (tmp_path / "stats.json").exists()
