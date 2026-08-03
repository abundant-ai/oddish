"""CLI coverage for operator concurrency get/set/clear commands."""

from __future__ import annotations

import json

import httpx
import pytest
from typer.testing import CliRunner

from oddish.cli import app
from oddish.cli import admin as admin_cli

runner = CliRunner()


def _setting(*, override=96, effective=96):
    return {
        "queue_key": "minimax/minimax-m3",
        "limit": override if override is not None else 64,
        "deploy_limit": 64,
        "override_limit": override,
        "controller_enabled": True,
        "advisory_limit": 80,
        "effective_limit": effective,
    }


class _Response:
    def __init__(self, status_code: int, payload):
        self.status_code = status_code
        self.payload = payload

    def json(self):
        return self.payload

    @property
    def text(self):
        return json.dumps(self.payload)


def _install_client(
    monkeypatch,
    *,
    get_response=None,
    put_response=None,
    queue_health_response=None,
    get_error: httpx.RequestError | None = None,
):
    calls = []

    class _Client:
        def __init__(self, *args, **kwargs):
            calls.append(("client", args, kwargs))

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def get(self, url, params=None):
            calls.append(("get", url, params))
            if get_error is not None:
                raise get_error
            if url.endswith("/admin/queue-health"):
                return queue_health_response or _Response(404, {})
            return get_response or _Response(200, _setting())

        def put(self, url, json=None):
            calls.append(("put", url, json))
            return put_response or _Response(200, _setting())

    monkeypatch.setattr(admin_cli.httpx, "Client", _Client)
    return calls


@pytest.fixture(autouse=True)
def api_key(monkeypatch):
    monkeypatch.setenv("ODDISH_API_KEY", "test-key")


def test_admin_concurrency_commands_are_registered():
    result = runner.invoke(app, ["admin", "concurrency", "--help"])

    assert result.exit_code == 0, result.output
    assert "get" in result.output
    assert "set" in result.output
    assert "clear" in result.output


def test_get_canonicalizes_queue_key_and_renders_all_limit_layers(monkeypatch):
    calls = _install_client(monkeypatch)

    result = runner.invoke(
        app,
        [
            "admin",
            "concurrency",
            "get",
            " MiniMax/MiniMax-M3 ",
            "--api-url",
            "http://api.test/",
        ],
    )

    assert result.exit_code == 0, result.output
    assert (
        "get",
        "http://api.test/admin/concurrency",
        {"queue_key": "minimax/minimax-m3"},
    ) in calls
    assert "minimax/minimax-m3" in result.output
    assert "Deploy" in result.output
    assert "DB override" in result.output
    assert "Controller" in result.output
    assert "Advisory" in result.output
    assert "Effective" in result.output


def test_get_json_emits_one_raw_document(monkeypatch):
    payload = _setting(effective=80)
    _install_client(monkeypatch, get_response=_Response(200, payload))

    result = runner.invoke(
        app,
        [
            "admin",
            "concurrency",
            "get",
            "minimax/minimax-m3",
            "--api-url",
            "http://api.test",
            "--json",
        ],
    )

    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout) == payload


def test_set_sends_override_then_reads_it_back(monkeypatch):
    payload = _setting(override=96, effective=80)
    calls = _install_client(
        monkeypatch,
        put_response=_Response(200, payload),
        get_response=_Response(200, payload),
    )

    result = runner.invoke(
        app,
        [
            "admin",
            "concurrency",
            "set",
            "MiniMax/MiniMax-M3",
            "96",
            "--api-url",
            "http://api.test",
            "--json",
        ],
    )

    assert result.exit_code == 0, result.output
    assert (
        "put",
        "http://api.test/admin/concurrency",
        {"queue_key": "minimax/minimax-m3", "limit": 96},
    ) in calls
    assert (
        "get",
        "http://api.test/admin/concurrency",
        {"queue_key": "minimax/minimax-m3"},
    ) in calls
    assert json.loads(result.stdout) == payload


def test_set_reads_back_through_queue_health_on_deployed_put_only_api(monkeypatch):
    old_put_payload = {
        "queue_key": "minimax/minimax-m3",
        "limit": 96,
        "deploy_limit": 64,
        "override_limit": 96,
    }
    queue_health = {
        "capacity": [
            {
                "queue_key": "minimax/minimax-m3",
                "limit": 96,
                "deploy_limit": 64,
                "override_limit": 96,
            }
        ]
    }
    calls = _install_client(
        monkeypatch,
        put_response=_Response(200, old_put_payload),
        get_response=_Response(404, {"detail": "Not Found"}),
        queue_health_response=_Response(200, queue_health),
    )

    result = runner.invoke(
        app,
        [
            "admin",
            "concurrency",
            "set",
            "MiniMax/MiniMax-M3",
            "96",
            "--api-url",
            "http://api.test",
            "--json",
        ],
    )

    assert result.exit_code == 0, result.output
    assert ("get", "http://api.test/admin/queue-health", None) in calls
    assert json.loads(result.stdout) == {
        "queue_key": "minimax/minimax-m3",
        "limit": 96,
        "deploy_limit": 64,
        "override_limit": 96,
        "controller_enabled": None,
        "advisory_limit": None,
        "effective_limit": None,
        "readback_source": "queue-health",
    }


def test_clear_sends_null_then_reads_back_deploy_fallback(monkeypatch):
    payload = _setting(override=None, effective=64)
    calls = _install_client(
        monkeypatch,
        put_response=_Response(200, payload),
        get_response=_Response(200, payload),
    )

    result = runner.invoke(
        app,
        [
            "admin",
            "concurrency",
            "clear",
            "MiniMax/MiniMax-M3",
            "--api-url",
            "http://api.test",
        ],
    )

    assert result.exit_code == 0, result.output
    assert (
        "put",
        "http://api.test/admin/concurrency",
        {"queue_key": "minimax/minimax-m3", "limit": None},
    ) in calls
    assert [call[0] for call in calls].count("get") == 1
    assert "64" in result.output


@pytest.mark.parametrize("limit", ["-1", "10001"])
def test_set_rejects_values_outside_server_range(monkeypatch, limit):
    calls = _install_client(monkeypatch)

    result = runner.invoke(
        app,
        [
            "admin",
            "concurrency",
            "set",
            "minimax/minimax-m3",
            limit,
            "--api-url",
            "http://api.test",
        ],
    )

    assert result.exit_code != 0
    assert not calls


def test_server_error_fails_clearly(monkeypatch):
    _install_client(
        monkeypatch,
        get_response=_Response(403, {"detail": "Operator organization required"}),
    )

    result = runner.invoke(
        app,
        [
            "admin",
            "concurrency",
            "get",
            "minimax/minimax-m3",
            "--api-url",
            "http://api.test",
        ],
    )

    assert result.exit_code == 1
    assert "Operator organization required" in result.output


def test_connection_error_fails_clearly(monkeypatch):
    _install_client(monkeypatch, get_error=httpx.ConnectError("network down"))

    result = runner.invoke(
        app,
        [
            "admin",
            "concurrency",
            "get",
            "minimax/minimax-m3",
            "--api-url",
            "http://api.test",
        ],
    )

    assert result.exit_code == 1
    assert "Failed to connect to API" in result.output


def test_mutation_readback_mismatch_fails(monkeypatch):
    mutation = _setting(override=96)
    readback = _setting(override=95, effective=95)
    _install_client(
        monkeypatch,
        put_response=_Response(200, mutation),
        get_response=_Response(200, readback),
    )

    result = runner.invoke(
        app,
        [
            "admin",
            "concurrency",
            "set",
            "minimax/minimax-m3",
            "96",
            "--api-url",
            "http://api.test",
        ],
    )

    assert result.exit_code == 1
    assert "did not match readback" in result.output
