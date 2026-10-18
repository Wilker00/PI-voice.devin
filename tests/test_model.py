import numpy as np

from server.model import _SHIFT_RATIOS, VoiceStream


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
