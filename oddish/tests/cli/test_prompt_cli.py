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
