from server import system


def _fake_vcgencmd(throttled):
    def run(*args):
        return {"measure_temp": "temp=43.3'C",
                "get_throttled": f"throttled={throttled}"}[args[0]]
    return run


def test_past_throttle_is_not_current(monkeypatch):
    monkeypatch.setattr(system, "_vcgencmd", _fake_vcgencmd("0x50000"))
    s = system.get_stats()
    assert s["on_pi"] and s["temp_c"] == 43.3
    assert s["throttled"] is False
    assert s["throttled_since_boot"] is True


def test_current_throttle(monkeypatch):
    monkeypatch.setattr(system, "_vcgencmd", _fake_vcgencmd("0x4"))
    s = system.get_stats()
    assert s["throttled"] is True and s["throttled_since_boot"] is False
