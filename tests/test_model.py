import numpy as np

from server.model import (_BANDS, _features, _SHIFT_RATIOS,
                          VoiceChangerModel, VoiceStream)


def test_voice_stream_defaults_to_clean():
    stream = VoiceStream(16000)
    assert stream.effect == "clean"


def test_character_presets_are_chunk_sized_and_finite():
    t = np.arange(1365, dtype=np.float32) / 16000
    chunk = (0.25 * np.sin(2 * np.pi * 220 * t)).astype(np.float32)
    for effect in ("clean", "deep", "robot", "chipmunk", "megaphone"):
        stream = VoiceStream(16000)
        stream.set_effect(effect)
        output = stream.convert(chunk, None)
        assert output.shape == chunk.shape
        assert np.isfinite(output).all()


def test_vader_wire_value_remains_deep():
    assert _SHIFT_RATIOS["deep"] == 0.749


def test_features_have_consistent_dim_for_short_clips():
    short = _features(np.zeros(100, np.float32), 16000)
    long = _features(np.zeros(16000, np.float32), 16000)
    assert short.shape == long.shape == (3 + _BANDS,)


def test_identify_skips_foreign_profile_shapes():
    m = VoiceChangerModel()
    t = np.arange(16000, dtype=np.float32) / 16000
    pcm = (0.3 * np.sin(2 * np.pi * 220 * t)).astype(np.float32)
    # VC-era raw-clip profiles and legacy dim-mismatched vectors must be
    # skipped, not crash the cosine dot.
    profiles = {"clip": np.zeros(160000, np.float32),
                "stub": np.zeros(10, np.float32)}
    assert m.identify(pcm, profiles) == ("unknown", 0.0)
    profiles["alice"] = m.embed(pcm)
    label, conf = m.identify(pcm, profiles)
    assert label == "alice" and conf > 0.99
