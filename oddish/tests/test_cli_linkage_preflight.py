from __future__ import annotations

import importlib
import sys
from pathlib import Path

import httpx
import pytest
import typer

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

api = importlib.import_module("oddish.cli.api")
run_mod = importlib.import_module("oddish.cli.run")


def _mock_client(handler, monkeypatch):
    """Point api.httpx.Client at a MockTransport running ``handler``."""
    real_client = httpx.Client

    def factory(*args, **kwargs):
        kwargs.pop("headers", None)
        kwargs.pop("timeout", None)
        return real_client(transport=httpx.MockTransport(handler), **kwargs)

    monkeypatch.setattr(api.httpx, "Client", factory)
    monkeypatch.setattr(api, "get_auth_headers", lambda *a, **k: {})


def test_helper_returns_true_only_on_linked_false(monkeypatch):
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        return httpx.Response(200, json={"linked": False})

    _mock_client(handler, monkeypatch)
    assert api.github_id_is_unlinked("http://api", "42") is True
    assert "actor_id=42" in captured["url"]
    assert captured["url"].endswith("/github/linkage?actor_id=42")


def test_helper_linked_true_fails_open(monkeypatch):
    _mock_client(lambda r: httpx.Response(200, json={"linked": True}), monkeypatch)
    assert api.github_id_is_unlinked("http://api", "42") is False


@pytest.mark.parametrize(
    "handler",
    [
        lambda r: httpx.Response(403, json={"detail": "Insufficient scope."}),
        lambda r: httpx.Response(500, text="boom"),
        lambda r: (_ for _ in ()).throw(httpx.ConnectError("no route")),
        lambda r: (_ for _ in ()).throw(httpx.ReadTimeout("slow")),
        lambda r: httpx.Response(200, text="not json"),
        lambda r: httpx.Response(200, json={"other": 1}),
    ],
)
def test_helper_failures_fail_open(monkeypatch, handler):
    _mock_client(handler, monkeypatch)
    assert api.github_id_is_unlinked("http://api", "42") is False


# --- run() integration: pre-flight gates the upload -------------------------


class _UploadSpy:
    def __init__(self):
        self.called = False

    def __call__(self, *args, **kwargs):
        self.called = True
        raise AssertionError("upload must not run after a failed pre-flight")


def _make_task_dir(tmp_path: Path) -> Path:
    task = tmp_path / "mytask"
    task.mkdir()
    (task / "task.toml").write_text("x")
    return task


def _run_env(monkeypatch):
    monkeypatch.setenv("ODDISH_API_KEY", "k")
    monkeypatch.setenv("ODDISH_API_URL", "http://api")


def _stub_task_resolution(monkeypatch, task_path: Path):
    # Keep the pre-flight the only thing under test: skip real task IO.
    monkeypatch.setattr(
        run_mod, "resolve_local_task_paths", lambda **kwargs: [task_path]
    )


def test_run_aborts_before_upload_when_unlinked(monkeypatch, tmp_path, capsys):
    _run_env(monkeypatch)
    task_path = _make_task_dir(tmp_path)
    _stub_task_resolution(monkeypatch, task_path)

    spy = _UploadSpy()
    monkeypatch.setattr(run_mod, "upload_tasks_with_progress", spy)
    monkeypatch.setattr(run_mod, "github_id_is_unlinked", lambda url, gid: True)

    with pytest.raises(typer.Exit) as exc:
        run_mod.run(path=task_path, github_id="42", watch=False)
    assert exc.value.exit_code == 1
    assert spy.called is False
    err = capsys.readouterr().err
    assert "GitHub account 42 is not connected to an oddish user" in err


def test_run_proceeds_to_upload_when_linked(monkeypatch, tmp_path):
    _run_env(monkeypatch)
    task_path = _make_task_dir(tmp_path)
    _stub_task_resolution(monkeypatch, task_path)

    calls = {"upload": 0}

    def fake_upload(api_url, task_paths, **kwargs):
        calls["upload"] += 1
        # Stop the run right after upload so we don't exercise the sweep path.
        raise typer.Exit(0)

    monkeypatch.setattr(run_mod, "upload_tasks_with_progress", fake_upload)
    monkeypatch.setattr(run_mod, "github_id_is_unlinked", lambda url, gid: False)

    with pytest.raises(typer.Exit):
        run_mod.run(path=task_path, github_id="42", watch=False)
    assert calls["upload"] == 1


@pytest.mark.parametrize("github_id", [None, "   "])
def test_run_absent_github_id_skips_preflight(monkeypatch, tmp_path, github_id):
    _run_env(monkeypatch)
    task_path = _make_task_dir(tmp_path)
    _stub_task_resolution(monkeypatch, task_path)

    calls = {"preflight": 0}

    def boom(url, gid):
        calls["preflight"] += 1
        raise AssertionError("pre-flight must not run for a blank github_id")

    monkeypatch.setattr(run_mod, "github_id_is_unlinked", boom)
    monkeypatch.setattr(
        run_mod,
        "upload_tasks_with_progress",
        lambda *a, **k: (_ for _ in ()).throw(typer.Exit(0)),
    )

    with pytest.raises(typer.Exit):
        run_mod.run(path=task_path, github_id=github_id, watch=False)
    assert calls["preflight"] == 0
