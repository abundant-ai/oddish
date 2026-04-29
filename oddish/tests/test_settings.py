from oddish.config import Settings


def test_settings_local_mode_defaults_false(monkeypatch):
    monkeypatch.delenv("ODDISH_LOCAL_MODE", raising=False)
    s = Settings()
    assert s.local_mode is False


def test_settings_local_mode_reads_env(monkeypatch):
    monkeypatch.setenv("ODDISH_LOCAL_MODE", "1")
    s = Settings()
    assert s.local_mode is True
