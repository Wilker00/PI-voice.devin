import numpy as np

from server.profiles import ProfileStore


def test_add_persist_reload(tmp_path):
    p = tmp_path / "profiles.json"
    store = ProfileStore(p)
    store.add("robbie", np.ones(11, dtype=np.float32))
    assert store.names() == ["robbie"]

    reloaded = ProfileStore(p)
    assert np.allclose(reloaded.embeddings()["robbie"], np.ones(11))


def test_remove(tmp_path):
    store = ProfileStore(tmp_path / "p.json")
    store.add("a", np.zeros(4))
    assert store.remove("a") is True
    assert store.remove("a") is False
    assert store.names() == []
