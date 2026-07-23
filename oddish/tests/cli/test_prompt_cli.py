import json

import httpx
from typer.testing import CliRunner

from oddish.cli.prompt import prompt_app

runner = CliRunner()


def _fake_client(monkeypatch, *, method, url_substr, status=200, payload=None):
    calls = {}

    class _Resp:
        status_code = status
        text = json.dumps(payload or {})

        def json(self):
            return payload or {}

    class _Client:
        def __init__(self, *a, **k):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def get(self, url, **k):
            calls["url"] = url
            return _Resp()

        def put(self, url, **k):
            calls["url"] = url
            calls["json"] = k.get("json")
            return _Resp()

        def post(self, url, **k):
            calls["url"] = url
            return _Resp()

    monkeypatch.setenv("ODDISH_API_KEY", "test-key")
    monkeypatch.setattr(httpx, "Client", _Client)
    return calls


def test_get_prints_content(monkeypatch):
    _fake_client(monkeypatch, method="get", url_substr="/prompts/pre_trial_qa",
                 payload={"key": "pre_trial_qa", "content": "HELLO"})
    result = runner.invoke(prompt_app, ["get", "pre_trial_qa"])
    assert result.exit_code == 0
    assert "HELLO" in result.stdout


def test_set_reads_file_and_puts(monkeypatch, tmp_path):
    calls = _fake_client(monkeypatch, method="put", url_substr="/prompts/pre_trial_qa",
                         payload={"key": "pre_trial_qa", "active_version": 2})
    f = tmp_path / "p.txt"
    f.write_text("NEW CONTENT")
    result = runner.invoke(prompt_app, ["set", "pre_trial_qa", "--file", str(f)])
    assert result.exit_code == 0
    assert calls["json"]["content"] == "NEW CONTENT"
    assert calls["url"].endswith("/prompts/pre_trial_qa")


def test_upload_is_the_primary_name(monkeypatch, tmp_path):
    calls = _fake_client(monkeypatch, method="put", url_substr="/prompts/pre_trial_qa",
                         payload={"key": "pre_trial_qa", "active_version": 2})
    f = tmp_path / "p.txt"
    f.write_text("NEW CONTENT")
    result = runner.invoke(prompt_app, ["upload", "pre_trial_qa", "--file", str(f)])
    assert result.exit_code == 0
    assert calls["json"]["content"] == "NEW CONTENT"
    assert calls["url"].endswith("/prompts/pre_trial_qa")


def test_update_is_a_hidden_alias(monkeypatch, tmp_path):
    calls = _fake_client(monkeypatch, method="put", url_substr="/prompts/pre_trial_qa",
                         payload={"key": "pre_trial_qa", "active_version": 2})
    f = tmp_path / "p.txt"
    f.write_text("NEW CONTENT")
    result = runner.invoke(prompt_app, ["update", "pre_trial_qa", "--file", str(f)])
    assert result.exit_code == 0
    assert calls["json"]["content"] == "NEW CONTENT"


def test_help_lists_upload_but_not_hidden_aliases():
    result = runner.invoke(prompt_app, ["--help"])
    assert result.exit_code == 0
    assert "upload" in result.stdout
    assert "update" not in result.stdout
    # "set" is a substring of no other command name here, so this is a safe check
    assert " set " not in result.stdout


def test_view_prints_usage_summary(monkeypatch):
    _fake_client(monkeypatch, method="get", url_substr="/prompts/pre_trial_qa",
                 payload={
                     "id": "p_abc123", "key": "pre_trial_qa", "active_version": 1,
                     "description": "d",
                     "usage": {"total": 0, "last_used_at": None, "by_version": []},
                 })
    result = runner.invoke(prompt_app, ["view", "pre_trial_qa"])
    assert result.exit_code == 0
    assert "pre_trial_qa" in result.stdout
    assert "not consumed by anything yet" in result.stdout


def test_view_prints_per_version_counts(monkeypatch):
    _fake_client(monkeypatch, method="get", url_substr="/prompts/pre_trial_qa",
                 payload={
                     "id": "p_abc123", "key": "pre_trial_qa", "active_version": 1,
                     "description": "d",
                     "usage": {
                         "total": 3, "last_used_at": "2026-07-22T00:00:00",
                         "by_version": [{"version": 1, "count": 3, "last_used_at": "2026-07-22T00:00:00"}],
                     },
                 })
    result = runner.invoke(prompt_app, ["view", "pre_trial_qa"])
    assert result.exit_code == 0
    assert "3 block(s)" in result.stdout
    assert "v1: 3 block(s)" in result.stdout


def test_list_output_includes_id(monkeypatch):
    _fake_client(monkeypatch, method="get", url_substr="/prompts",
                 payload=[{"id": "p_abc123", "key": "pre_trial_qa", "active_version": 1, "description": "d"}])
    result = runner.invoke(prompt_app, ["list"])
    assert result.exit_code == 0
    assert "p_abc123" in result.stdout
