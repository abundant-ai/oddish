from __future__ import annotations

from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from oddish.config import Settings  # noqa: E402


def test_env_local_is_loaded(tmp_path, monkeypatch):
    """A value present only in .env.local is picked up for local backend runs."""
    (tmp_path / ".env.local").write_text("ODDISH_HARBOR_JOBS_DIR=/from-env-local\n")
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("ODDISH_HARBOR_JOBS_DIR", raising=False)

    settings = Settings()

    assert settings.harbor_jobs_dir == "/from-env-local"


def test_env_local_overrides_dotenv(tmp_path, monkeypatch):
    """.env.local layers over .env -- the later file wins on duplicate keys."""
    (tmp_path / ".env").write_text("ODDISH_HARBOR_JOBS_DIR=/from-dotenv\n")
    (tmp_path / ".env.local").write_text("ODDISH_HARBOR_JOBS_DIR=/from-env-local\n")
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("ODDISH_HARBOR_JOBS_DIR", raising=False)

    settings = Settings()

    assert settings.harbor_jobs_dir == "/from-env-local"


def test_real_env_var_still_outranks_env_local(tmp_path, monkeypatch):
    """An exported env var must still win over .env.local (no precedence regression)."""
    (tmp_path / ".env.local").write_text("ODDISH_HARBOR_JOBS_DIR=/from-env-local\n")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("ODDISH_HARBOR_JOBS_DIR", "/from-process-env")

    settings = Settings()

    assert settings.harbor_jobs_dir == "/from-process-env"
