"""`oddish qa-jobs` flag handling and request shaping.

The HTTP layer is faked; what matters here is which scope each command sends,
since PR 1 shipped a bug where writes defaulted to org and reads to global with
no signal to the user.
"""

import json

import httpx
from typer.testing import CliRunner

from oddish.cli.qa_jobs import qa_jobs_app

runner = CliRunner()

_JOB = {
    "id": "qa_1",
    "stage": "post_trial",
    "prompt_id": "p1",
    "prompt_kind": "QA_POST_TRIAL",
    "prompt_version": None,
    "effective_version": 9,
    "scope_type": "task",
    "scope_id": "fvsmith",
    "org_id": "org_a",
    "model": "claude-opus-4-8",
    "reasoning_effort": None,
    "backend": "api",
    "allow_oddish_cli": False,
    "enabled": True,
    "inherited_from": "task:fvsmith",
}


def _fake_client(monkeypatch, *, payload, status=200):
    calls: dict = {"requests": []}

    class _Resp:
        status_code = status
        text = json.dumps(payload)

        def json(self):
            return payload

    class _Client:
        def __init__(self, *a, **k):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def _record(self, verb, url, k):
            calls["requests"].append(
                {
                    "verb": verb,
                    "url": url,
                    "params": k.get("params") or {},
                    "json": k.get("json"),
                }
            )
            return _Resp()

        def get(self, url, **k):
            return self._record("GET", url, k)

        def put(self, url, **k):
            return self._record("PUT", url, k)

        def post(self, url, **k):
            return self._record("POST", url, k)

    monkeypatch.setenv("ODDISH_API_KEY", "test-key")
    monkeypatch.setattr(httpx, "Client", _Client)
    return calls


# ---------------------------------------------------------------------------
# Stage flags
# ---------------------------------------------------------------------------


def test_stage_is_required(monkeypatch):
    _fake_client(monkeypatch, payload=_JOB)
    result = runner.invoke(qa_jobs_app, ["assign", "QA_POST_TRIAL"])
    assert result.exit_code != 0


def test_both_stages_rejected(monkeypatch):
    _fake_client(monkeypatch, payload=_JOB)
    result = runner.invoke(
        qa_jobs_app, ["assign", "QA_POST_TRIAL", "--pre-trial", "--post-trial"]
    )
    assert result.exit_code != 0


def test_multiple_scope_flags_rejected(monkeypatch):
    _fake_client(monkeypatch, payload=_JOB)
    result = runner.invoke(
        qa_jobs_app,
        ["assign", "QA_POST_TRIAL", "--post-trial", "--org", "--task", "t1"],
    )
    assert result.exit_code != 0


# ---------------------------------------------------------------------------
# Scope defaults: writes -> org, reads -> global
# ---------------------------------------------------------------------------


def test_assign_defaults_to_org_scope(monkeypatch):
    calls = _fake_client(monkeypatch, payload=_JOB)
    result = runner.invoke(qa_jobs_app, ["assign", "QA_POST_TRIAL", "--post-trial"])
    assert result.exit_code == 0, result.output
    post = calls["requests"][-1]
    assert post["verb"] == "POST"
    assert post["params"]["scope"] == "org"
    assert "scope_id" not in post["params"]


def test_list_defaults_to_global_scope(monkeypatch):
    calls = _fake_client(monkeypatch, payload=[_JOB])
    result = runner.invoke(qa_jobs_app, ["list"])
    assert result.exit_code == 0, result.output
    assert calls["requests"][-1]["params"]["scope"] == "global"


def test_list_reports_the_scope_it_read(monkeypatch):
    """Reads default to global while writes default to org, so an unlabelled
    listing silently shows pre-edit state -- exactly the PR 1 bug."""
    _fake_client(monkeypatch, payload=[_JOB])
    result = runner.invoke(qa_jobs_app, ["list"])
    assert result.exit_code == 0
    assert "scope=global" in result.output


def test_list_task_scope_sends_scope_id_and_reports_it(monkeypatch):
    calls = _fake_client(monkeypatch, payload=[_JOB])
    result = runner.invoke(qa_jobs_app, ["list", "--task", "fvsmith"])
    assert result.exit_code == 0, result.output
    params = calls["requests"][-1]["params"]
    assert params["scope"] == "task"
    assert params["scope_id"] == "fvsmith"
    assert "scope=task:fvsmith" in result.output


def test_list_defaults_to_resolved_view(monkeypatch):
    calls = _fake_client(monkeypatch, payload=[_JOB])
    runner.invoke(qa_jobs_app, ["list", "--task", "fvsmith"])
    assert calls["requests"][-1]["params"]["resolved"] == "true"


def test_defined_flag_requests_exact_scope_rows(monkeypatch):
    calls = _fake_client(monkeypatch, payload=[_JOB])
    runner.invoke(qa_jobs_app, ["list", "--task", "fvsmith", "--defined"])
    assert calls["requests"][-1]["params"]["resolved"] == "false"


def test_list_renders_inherited_scope(monkeypatch):
    _fake_client(monkeypatch, payload=[_JOB])
    result = runner.invoke(qa_jobs_app, ["list", "--task", "fvsmith"])
    assert "QA_POST_TRIAL" in result.output
    assert "task:fvsmith" in result.output


# ---------------------------------------------------------------------------
# assign --file uploads a version first
# ---------------------------------------------------------------------------


def test_assign_with_file_uploads_then_assigns_at_same_scope(monkeypatch, tmp_path):
    payload = dict(_JOB, kind="QA_POST_TRIAL", latest_version=8)
    calls = _fake_client(monkeypatch, payload=payload)
    prompt_file = tmp_path / "post_trial.md"
    prompt_file.write_text("NEW BODY")

    result = runner.invoke(
        qa_jobs_app,
        [
            "assign",
            "QA_POST_TRIAL",
            "--post-trial",
            "--task",
            "fvsmith",
            "--file",
            str(prompt_file),
            "--backend",
            "api",
            "--model",
            "claude-opus-4-8",
        ],
    )
    assert result.exit_code == 0, result.output
    put, post = calls["requests"]
    assert put["verb"] == "PUT" and put["url"].endswith("/prompts/QA_POST_TRIAL")
    assert put["json"]["content"] == "NEW BODY"
    assert put["params"] == {"scope": "task", "scope_id": "fvsmith"}
    # The assignment must land at the same scope the version was written to.
    assert post["verb"] == "POST" and post["params"] == put["params"]
    assert post["json"]["model"] == "claude-opus-4-8"
    assert post["json"]["stage"] == "post_trial"


def test_assign_without_file_makes_no_prompt_write(monkeypatch):
    calls = _fake_client(monkeypatch, payload=_JOB)
    runner.invoke(
        qa_jobs_app, ["assign", "QA_POST_TRIAL", "--post-trial", "--task", "fvsmith"]
    )
    assert [r["verb"] for r in calls["requests"]] == ["POST"]


def test_assign_omits_model_so_server_defaults_apply(monkeypatch):
    calls = _fake_client(monkeypatch, payload=_JOB)
    runner.invoke(qa_jobs_app, ["assign", "QA_PRE_TRIAL", "--pre-trial"])
    assert calls["requests"][-1]["json"]["model"] is None
    assert calls["requests"][-1]["json"]["backend"] is None


def test_bad_backend_rejected(monkeypatch):
    _fake_client(monkeypatch, payload=_JOB)
    result = runner.invoke(
        qa_jobs_app, ["assign", "QA_POST_TRIAL", "--post-trial", "--backend", "carrier"]
    )
    assert result.exit_code != 0


# ---------------------------------------------------------------------------
# disable
# ---------------------------------------------------------------------------


def test_disable_writes_a_disabled_row_at_the_named_scope(monkeypatch):
    calls = _fake_client(monkeypatch, payload=dict(_JOB, enabled=False))
    result = runner.invoke(
        qa_jobs_app, ["disable", "QA_POST_TRIAL", "--post-trial", "--task", "fvsmith"]
    )
    assert result.exit_code == 0, result.output
    post = calls["requests"][-1]
    assert post["json"] == {
        "prompt": "QA_POST_TRIAL",
        "stage": "post_trial",
        "enabled": False,
    }
    assert post["params"] == {"scope": "task", "scope_id": "fvsmith"}


def test_failed_request_exits_nonzero(monkeypatch):
    _fake_client(monkeypatch, payload={"detail": "nope"}, status=404)
    result = runner.invoke(
        qa_jobs_app, ["assign", "QA_POST_TRIAL", "--post-trial", "--task", "fvsmith"]
    )
    assert result.exit_code == 1
