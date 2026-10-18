import numpy as np

from server.dsp import PitchShifter, RingMod, estimate_f0, megaphone

SR = 16000


def tone(seconds, freq, sr=SR):
    t = np.arange(int(seconds * sr)) / sr
    return (0.3 * np.sin(2 * np.pi * freq * t)).astype(np.float32)


def dominant_freq(x, sr=SR):
    mag = np.abs(np.fft.rfft(x * np.hanning(x.size)))
    freqs = np.fft.rfftfreq(x.size, 1.0 / sr)
    return freqs[int(np.argmax(mag[1:])) + 1]


def test_estimate_f0_sine():
    f0 = estimate_f0(tone(0.5, 220.0), SR)
    assert f0 is not None and abs(f0 - 220.0) < 15


def test_estimate_f0_silence():
    assert estimate_f0(np.zeros(SR // 2, dtype=np.float32), SR) is None


def test_pitch_shifter_moves_dominant_freq():
    sh = PitchShifter(SR, ratio=1.414)
    # let it settle, then measure a steady-state window
    for _ in range(6):
        out = sh.process(tone(0.25, 220.0))
    f_out = dominant_freq(out)
    assert abs(f_out - 220.0 * 1.414) / 220.0 < 0.2


def test_pitch_shifter_preserves_length_and_bounds():
    sh = PitchShifter(SR, ratio=0.749)
    x = tone(0.1, 220.0)
    out = sh.process(x)
    assert out.size == x.size
    assert np.isfinite(out).all()


def test_ringmod_is_phase_continuous():
    rm = RingMod(SR, 30.0)
    a = rm.process(tone(0.05, 220.0))
    b = rm.process(tone(0.05, 220.0))
    assert a.size == b.size == 800
    # adjacent chunks shouldn't be identical (carrier keeps moving)
    assert not np.allclose(a, b)


def test_megaphone_shape():
    x = tone(0.05, 220.0)
    assert megaphone(x).size == x.size
