import numpy as np

from server.profiles import ProfileStore


def test_add_persist_reload(tmp_path):
    p = tmp_path / "profiles.json"
    store = ProfileStore(p)
    store.add("robbie", np.ones(11, dtype=np.float32), f0=185.5)
    assert store.names() == ["robbie"]

    reloaded = ProfileStore(p)
    prof = reloaded.get("robbie")
    assert np.allclose(prof["emb"], np.ones(11))
    assert prof["f0"] == 185.5
    assert np.allclose(reloaded.embeddings()["robbie"], np.ones(11))


def test_legacy_bare_embedding_format(tmp_path):
    p = tmp_path / "p.json"
    p.write_text('{"a": [1.0, 2.0, 3.0]}')
    store = ProfileStore(p)
    assert np.allclose(store.get("a")["emb"], [1.0, 2.0, 3.0])
    assert store.get("a")["f0"] is None


def test_remove(tmp_path):
    store = ProfileStore(tmp_path / "p.json")
    store.add("a", np.zeros(4))
    assert store.remove("a") is True
    assert store.remove("a") is False
    assert store.names() == []
