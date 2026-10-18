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
