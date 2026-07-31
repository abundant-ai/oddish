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
    _fake_client(
        monkeypatch,
        method="get",
        url_substr="/prompts/pre_trial_qa",
        payload={"key": "pre_trial_qa", "content": "HELLO"},
    )
    result = runner.invoke(prompt_app, ["get", "pre_trial_qa"])
    assert result.exit_code == 0
    assert "HELLO" in result.stdout


def test_set_reads_file_and_puts(monkeypatch, tmp_path):
    calls = _fake_client(
        monkeypatch,
        method="put",
        url_substr="/prompts/pre_trial_qa",
        payload={"key": "pre_trial_qa", "active_version": 2},
    )
    f = tmp_path / "p.txt"
    f.write_text("NEW CONTENT")
    result = runner.invoke(prompt_app, ["set", "pre_trial_qa", "--file", str(f)])
    assert result.exit_code == 0
    assert calls["json"]["content"] == "NEW CONTENT"
    assert calls["url"].endswith("/prompts/pre_trial_qa")


def test_upload_is_the_primary_name(monkeypatch, tmp_path):
    calls = _fake_client(
        monkeypatch,
        method="put",
        url_substr="/prompts/pre_trial_qa",
        payload={"key": "pre_trial_qa", "active_version": 2},
    )
    f = tmp_path / "p.txt"
    f.write_text("NEW CONTENT")
    result = runner.invoke(prompt_app, ["upload", "pre_trial_qa", "--file", str(f)])
    assert result.exit_code == 0
    assert calls["json"]["content"] == "NEW CONTENT"
    assert calls["url"].endswith("/prompts/pre_trial_qa")


def test_update_is_a_hidden_alias(monkeypatch, tmp_path):
    calls = _fake_client(
        monkeypatch,
        method="put",
        url_substr="/prompts/pre_trial_qa",
        payload={"key": "pre_trial_qa", "active_version": 2},
    )
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
    _fake_client(
        monkeypatch,
        method="get",
        url_substr="/prompts/pre_trial_qa",
        payload={
            "id": "p_abc123",
            "kind": "pre_trial_qa",
            "latest_version": 1,
            "description": "d",
            "usage": {"total": 0, "last_used_at": None, "by_version": []},
        },
    )
    result = runner.invoke(prompt_app, ["view", "pre_trial_qa"])
    assert result.exit_code == 0
    assert "pre_trial_qa" in result.stdout
    assert "not consumed by anything yet" in result.stdout


def test_view_prints_per_version_counts(monkeypatch):
    _fake_client(
        monkeypatch,
        method="get",
        url_substr="/prompts/pre_trial_qa",
        payload={
            "id": "p_abc123",
            "kind": "pre_trial_qa",
            "latest_version": 1,
            "description": "d",
            "usage": {
                "total": 3,
                "last_used_at": "2026-07-22T00:00:00",
                "by_version": [
                    {"version": 1, "count": 3, "last_used_at": "2026-07-22T00:00:00"}
                ],
            },
        },
    )
    result = runner.invoke(prompt_app, ["view", "pre_trial_qa"])
    assert result.exit_code == 0
    assert "3 block(s)" in result.stdout
    assert "v1: 3 block(s)" in result.stdout


def test_seed_sends_global_scope_on_get_and_put(monkeypatch):
    """`oddish prompt seed` must probe and write the installation-wide row
    explicitly, not rely on each endpoint's default (GET defaults to global
    already, but PUT defaults to org -- an implicit PUT would silently create
    an org override and never seed the global row the GET just checked for)."""
    from oddish.core.prompt_seeds import PROMPT_SEEDS

    get_calls = []
    put_calls = []

    class _GetResp:
        status_code = 404
        text = "{}"

        def json(self):
            return {}

    class _PutResp:
        status_code = 200
        text = "{}"

        def json(self):
            return {"latest_version": 1}

    class _Client:
        def __init__(self, *a, **k):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def get(self, url, **k):
            get_calls.append({"url": url, "params": k.get("params")})
            return _GetResp()

        def put(self, url, **k):
            put_calls.append({"url": url, "params": k.get("params")})
            return _PutResp()

    monkeypatch.setenv("ODDISH_API_KEY", "test-key")
    monkeypatch.setattr(httpx, "Client", _Client)

    result = runner.invoke(prompt_app, ["seed"])

    assert result.exit_code == 0
    assert len(get_calls) == len(PROMPT_SEEDS)
    assert len(put_calls) == len(PROMPT_SEEDS)
    for call in get_calls + put_calls:
        assert call["params"] == {"scope": "global"}


def test_get_shows_scope_on_screen(monkeypatch):
    """A --global read of a kind that also has an org override must not look
    identical to that override: the resolved scope has to be visible."""
    _fake_client(
        monkeypatch,
        method="get",
        url_substr="/prompts/QA_POST_TRIAL",
        payload={"content": "GLOBAL TEXT", "scope_type": None, "scope_id": None},
    )
    result = runner.invoke(prompt_app, ["get", "QA_POST_TRIAL"])
    assert result.exit_code == 0
    assert "scope=global" in result.output
    assert "GLOBAL TEXT" in result.output


def test_list_shows_scope_for_each_prompt(monkeypatch):
    _fake_client(
        monkeypatch,
        method="get",
        url_substr="/prompts",
        payload=[
            {
                "kind": "QA_POST_TRIAL",
                "id": "global",
                "latest_version": 1,
                "scope_type": None,
                "scope_id": None,
            },
            {
                "kind": "QA_POST_TRIAL",
                "id": "task",
                "latest_version": 2,
                "scope_type": "task",
                "scope_id": "task_1",
            },
        ],
    )
    result = runner.invoke(prompt_app, ["list"])
    assert result.exit_code == 0
    assert "scope=global" in result.output
    assert "scope=task:task_1" in result.output


def test_get_shows_org_scope(monkeypatch):
    _fake_client(
        monkeypatch,
        method="get",
        url_substr="/prompts/QA_POST_TRIAL",
        payload={
            "content": "ORG TEXT",
            "scope_type": "org",
            "scope_id": "org_1",
        },
    )
    result = runner.invoke(prompt_app, ["get", "QA_POST_TRIAL", "--org"])
    assert result.exit_code == 0
    assert "scope=org:org_1" in result.output


def test_view_shows_scope(monkeypatch):
    _fake_client(
        monkeypatch,
        method="get",
        url_substr="/prompts/pre_trial_qa",
        payload={
            "id": "p_abc123",
            "kind": "pre_trial_qa",
            "latest_version": 1,
            "description": "d",
            "scope_type": "task",
            "scope_id": "task_1",
            "usage": {"total": 0, "last_used_at": None, "by_version": []},
        },
    )
    result = runner.invoke(prompt_app, ["view", "pre_trial_qa", "--task", "task_1"])
    assert result.exit_code == 0
    assert "scope=task:task_1" in result.output


def test_versions_shows_requested_scope(monkeypatch):
    calls = _fake_client(
        monkeypatch,
        method="get",
        url_substr="/prompts/pre_trial_qa/versions",
        payload=[{"version": 1, "created_at": "", "created_by": None}],
    )
    result = runner.invoke(prompt_app, ["versions", "pre_trial_qa", "--org"])
    assert result.exit_code == 0
    assert "scope=org" in result.output
    assert calls["url"].endswith("/prompts/pre_trial_qa/versions")


def test_diff_sends_the_requested_scope_to_both_lookups(monkeypatch):
    calls = []

    class _Resp:
        status_code = 200
        text = "{}"

        def __init__(self, content):
            self._content = content

        def json(self):
            return {"content": self._content}

    class _Client:
        def __init__(self, *a, **k):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def get(self, url, **k):
            calls.append({"url": url, "params": k.get("params")})
            return _Resp(f"v{k['params']['version']}")

    monkeypatch.setenv("ODDISH_API_KEY", "test-key")
    monkeypatch.setattr(httpx, "Client", _Client)

    result = runner.invoke(
        prompt_app, ["diff", "pre_trial_qa", "1", "2", "--task", "task_1"]
    )

    assert result.exit_code == 0
    assert len(calls) == 2
    for call in calls:
        assert call["params"]["scope"] == "task"
        assert call["params"]["scope_id"] == "task_1"
    assert "scope=task:task_1" in result.output


def test_diff_defaults_to_global_like_the_other_reads(monkeypatch):
    calls = []

    class _Resp:
        status_code = 200
        text = "{}"

        def __init__(self, content):
            self._content = content

        def json(self):
            return {"content": self._content}

    class _Client:
        def __init__(self, *a, **k):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def get(self, url, **k):
            calls.append({"url": url, "params": k.get("params")})
            return _Resp(f"v{k['params']['version']}")

    monkeypatch.setenv("ODDISH_API_KEY", "test-key")
    monkeypatch.setattr(httpx, "Client", _Client)

    result = runner.invoke(prompt_app, ["diff", "pre_trial_qa", "1", "2"])

    assert result.exit_code == 0
    for call in calls:
        assert call["params"]["scope"] == "global"
        assert "scope_id" not in call["params"]


def test_list_output_includes_id(monkeypatch):
    _fake_client(
        monkeypatch,
        method="get",
        url_substr="/prompts",
        payload=[
            {
                "id": "p_abc123",
                "kind": "pre_trial_qa",
                "latest_version": 1,
                "description": "d",
            }
        ],
    )
    result = runner.invoke(prompt_app, ["list"])
    assert result.exit_code == 0
    assert "p_abc123" in result.stdout
