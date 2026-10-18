import numpy as np

from server.audio import RingBuffer, float32_to_pcm16, pcm16_to_float32, resample_linear


def test_pcm16_round_trip():
    x = np.array([0.0, 0.5, -0.5, 0.999, -1.0], dtype=np.float32)
    back = pcm16_to_float32(float32_to_pcm16(x))
    assert np.allclose(back, x, atol=1e-3)


def test_resample_doubles_length():
    x = np.sin(np.linspace(0, 10, 4800)).astype(np.float32)
    out = resample_linear(x, 48000, 16000)
    assert out.size == 1600
    assert out.dtype == np.float32
    same = resample_linear(x, 16000, 16000)
    assert np.array_equal(same, x)


def test_ring_buffer_in_order_under_capacity():
    rb = RingBuffer(8)
    rb.write(np.arange(5, dtype=np.float32))
    assert len(rb) == 5
    assert np.array_equal(rb.read_last(5), np.arange(5, dtype=np.float32))
    assert rb.read_last(10).size == 5  # only what's filled


def test_ring_buffer_wraps_and_overwrites_oldest():
    rb = RingBuffer(8)
    rb.write(np.arange(6, dtype=np.float32))
    rb.write(np.arange(6, 11, dtype=np.float32))  # wraps around
    assert len(rb) == 8
    assert np.array_equal(rb.read_last(8), np.arange(3, 11, dtype=np.float32))


def test_ring_buffer_oversized_write_keeps_tail():
    rb = RingBuffer(4)
    rb.write(np.arange(10, dtype=np.float32))
    assert np.array_equal(rb.read_last(4), np.array([6, 7, 8, 9], dtype=np.float32))


def test_stream_resampler_is_chunk_continuous():
    from server.audio import StreamResampler
    x = np.sin(2 * np.pi * 300 * np.arange(48000) / 48000).astype(np.float32)
    whole = StreamResampler(48000, 16000).process(x)
    rs = StreamResampler(48000, 16000)
    parts = np.concatenate([rs.process(x[i:i + 4096])
                            for i in range(0, x.size, 4096)])
    assert abs(parts.size - 16000) <= 1
    assert np.allclose(parts, whole[:parts.size], atol=1e-5)


def test_stream_resampler_upsamples_odd_chunks():
    from server.audio import StreamResampler
    rs = StreamResampler(16000, 44100)
    out = np.concatenate([rs.process(np.ones(n, dtype=np.float32))
                          for n in (1, 3, 1365, 1, 1000)])
    assert abs(out.size - round(2370 * 44100 / 16000)) <= 2


def test_stream_resampler_rejects_aliasing_tone():
    from server.audio import StreamResampler
    # 12 kHz is above the 8 kHz Nyquist of 16k output: must be filtered,
    # not folded back down to 4 kHz.
    t = np.arange(48000) / 48000
    tone = np.sin(2 * np.pi * 12000 * t).astype(np.float32)
    out = StreamResampler(48000, 16000).process(tone)[100:]
    naive = resample_linear(tone, 48000, 16000)[100:]
    assert np.sqrt(np.mean(out ** 2)) < 0.05
    assert np.sqrt(np.mean(naive ** 2)) > 0.3
