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


def test_pitch_shifter_chunk_in_chunk_out_without_gaps():
    # One chunk in -> one same-sized chunk out, at any ratio and even with
    # tiny chunks; the primed output queue never runs dry mid-stream.
    for ratio in (0.6, 0.749, 1.414, 1.7):
        sh = PitchShifter(SR, ratio=ratio)
        x = tone(3.0, 220.0)
        for i in range(0, x.size, 128):
            out = sh.process(x[i:i + 128])
            assert out.size == x[i:i + 128].size
            assert np.isfinite(out).all()
        assert sh.underruns == 0


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


def _shift_in_chunks(ratio, x, chunk):
    sh = PitchShifter(SR, ratio=ratio)
    return np.concatenate([sh.process(x[i:i + chunk])
                           for i in range(0, x.size, chunk)]
                          + [sh.process(np.zeros(SR // 10, np.float32))])


def test_pitch_shifter_output_independent_of_chunk_size():
    # Phone frames arrive as ~1365-sample chunks at 16 kHz. Chunking must
    # not change the audio (this caught a junk-sample bug on empty frames).
    x = tone(1.0, 180.0)
    ref = _shift_in_chunks(0.749, x, x.size)
    for chunk in (128, 1365, 4096):
        assert np.allclose(_shift_in_chunks(0.749, x, chunk), ref, atol=1e-5)


def test_pitch_shifter_keeps_every_syllable_in_order():
    # 5 short bursts in -> 5 bursts out, same spacing (+ fixed latency):
    # nothing dropped, nothing replayed.
    x = np.zeros(2 * SR, dtype=np.float32)
    times = [0.20, 0.55, 0.90, 1.25, 1.60]
    for t in times:
        x[int(t * SR):int((t + 0.03) * SR)] = tone(0.03, 300.0)
    y = _shift_in_chunks(0.749, x, 1365)
    env = np.convolve(np.abs(y), np.ones(160) / 160, "same")
    above = env > env.max() * 0.3
    starts = (np.flatnonzero(above[1:] & ~above[:-1]) + 1) / SR
    # Overlapping frames can smear a sharp burst into a close double;
    # merge those into one syllable.
    starts = starts[np.concatenate([[True], np.diff(starts) > 0.1])]
    assert len(starts) == len(times)
    # same spacing as the input: nothing dropped, stretched or replayed
    assert np.allclose(np.diff(starts), np.diff(times), atol=0.02)


def test_estimate_f0_median_ignores_pauses():
    from server.dsp import estimate_f0_median
    gap = np.zeros(SR // 2, dtype=np.float32)
    clip = np.concatenate([tone(0.4, 200.0), gap, tone(0.4, 210.0), gap,
                           tone(0.4, 205.0)])
    assert abs(estimate_f0_median(clip, SR) - 205.0) < 10


def test_pitch_shifter_has_no_warble():
    # Steady voice in -> steady loudness out (30 ms RMS, dB std). The old
    # two-tap shifter measured ~1-1.5 dB here from tap cancellation.
    t = np.arange(3 * SR) / SR
    x = (0.2 * sum(np.sin(2 * np.pi * 150 * k * t) / k for k in range(1, 8))
         ).astype(np.float32)
    for ratio in (0.749, 1.414):
        y = _shift_in_chunks(ratio, x, 1365)[SR // 2:2 * SR]
        w = int(0.03 * SR)
        e = np.sqrt([np.mean(y[i:i + w] ** 2) for i in range(0, y.size - w, w)])
        assert np.std(20 * np.log10(e / np.median(e))) < 0.4
