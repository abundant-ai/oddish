import os
from oddish.config import Settings


def test_daytona_api_key_reads_from_env(monkeypatch):
    monkeypatch.setenv("DAYTONA_API_KEY", "dt_test_value")
    s = Settings(_env_file=None)
    assert s.daytona_api_key == "dt_test_value"


def test_daytona_api_key_defaults_to_none(monkeypatch):
    monkeypatch.delenv("DAYTONA_API_KEY", raising=False)
    s = Settings(_env_file=None)
    assert s.daytona_api_key is None


def test_cc_chat_local_jobs_dir_reads_from_env(monkeypatch):
    monkeypatch.setenv("ODDISH_CC_CHAT_LOCAL_JOBS_DIR", "/tmp/fake/jobs")
    s = Settings(_env_file=None)
    assert s.cc_chat_local_jobs_dir == "/tmp/fake/jobs"
