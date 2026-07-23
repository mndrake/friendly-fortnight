import pytest

from lineage.config import ConfigError, from_dict


def _minimal() -> dict:
    return {
        "scratch_lib": "QTEMP",
        "libraries": ["APPLIB"],
        "output_seeds": [{"id": "X", "library": "APPLIB", "file": "OUT"}],
    }


def test_minimal_config_loads():
    cfg = from_dict(_minimal())
    assert cfg.scratch_lib == "QTEMP"
    assert cfg.libraries == ("APPLIB",)
    assert cfg.output_seeds[0].node_id == "file:APPLIB/OUT"


def test_missing_libraries_rejected():
    raw = _minimal()
    raw["libraries"] = []
    with pytest.raises(ConfigError):
        from_dict(raw)


def test_missing_seeds_rejected():
    raw = _minimal()
    raw["output_seeds"] = []
    with pytest.raises(ConfigError):
        from_dict(raw)


def test_duplicate_seed_ids_rejected():
    raw = _minimal()
    raw["output_seeds"] = [
        {"id": "X", "library": "A", "file": "F1"},
        {"id": "X", "library": "A", "file": "F2"},
    ]
    with pytest.raises(ConfigError):
        from_dict(raw)


def test_liblist_fallbacks():
    raw = _minimal()
    raw["liblists"] = {"default": ["A", "B"], "BATCH": ["C"]}
    cfg = from_dict(raw)
    assert cfg.liblist("BATCH") == ("C",)
    assert cfg.liblist("NOPE") == ("A", "B")
    assert cfg.liblist(None) == ("A", "B")


def test_liblist_falls_back_to_libraries_without_default():
    cfg = from_dict(_minimal())
    assert cfg.liblist(None) == ("APPLIB",)


def test_password_env_wins(monkeypatch):
    raw = _minimal()
    raw["connection"] = {"host": "H", "password": "stored"}
    cfg = from_dict(raw)
    monkeypatch.setenv("LINEAGE_DB_PASSWORD", "env")
    assert cfg.connection.resolved_password() == "env"
    monkeypatch.delenv("LINEAGE_DB_PASSWORD")
    assert cfg.connection.resolved_password() == "stored"


def test_url_templated_from_host():
    raw = _minimal()
    raw["connection"] = {"host": "MYHOST"}
    assert from_dict(raw).connection.resolved_url() == "jdbc:as400://MYHOST"
