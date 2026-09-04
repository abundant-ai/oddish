"""``oddish run`` sends the model id the user typed; the API resolves it.

The CLI used to call ``auto_resolve_curated_model`` itself and replace ``-m``
with an explicit ``fireworks/`` or ``deepseek/`` prefix. That was wrong twice
over: the choice came from the *user machine's* provider keys, which say
nothing about Oddish Cloud's credentials, and the explicit prefix left the
server nothing to correct. It also printed the choice to stdout, so
``run --json`` emitted a preamble line ahead of its JSON document and
``json.loads(stdout)`` failed.

The client now submits the raw spelling and the server owns canonicalization,
provider choice, and the curated allowlist rejection.
"""

from __future__ import annotations

import importlib
import itertools
import json
import sys
from pathlib import Path

import pytest
from typer.testing import CliRunner

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from oddish.cli import app  # noqa: E402

run_mod = importlib.import_module("oddish.cli.run")
api_mod = importlib.import_module("oddish.cli.api")

_CRED_ENV = ("FIREWORKS_API_KEY", "DEEPSEEK_API_KEY")
_CRED_COMBOS = tuple(itertools.product((False, True), repeat=len(_CRED_ENV)))

_SWEEP_RESULT = {
    "id": "task-1",
    "trials_count": 1,
    "experiment_id": "exp-1",
    "experiment_name": "abt-1153",
    "providers": {"fireworks": 1},
}


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    monkeypatch.setenv("ODDISH_API_KEY", "ok_test")
    monkeypatch.setenv("ODDISH_API_URL", "http://api.example")
    monkeypatch.delenv("ODDISH_MODEL_CATALOG_OVERLAY", raising=False)
    monkeypatch.delenv("ODDISH_ENFORCE_MODEL_CREDENTIALS", raising=False)
    for name in _CRED_ENV:
        monkeypatch.delenv(name, raising=False)


def _apply_credentials(monkeypatch, combo: tuple[bool, ...]) -> None:
    for name, present in zip(_CRED_ENV, combo):
        if present:
            monkeypatch.setenv(name, f"sk-test-{name.lower()}")
        else:
            monkeypatch.delenv(name, raising=False)


def _capture_payloads(monkeypatch) -> list[dict]:
    """Intercept the sweep POST and record the outgoing body."""
    payloads: list[dict] = []

    def _post(api_url, payload):
        payloads.append(payload)
        return dict(_SWEEP_RESULT)

    monkeypatch.setattr(run_mod, "post_sweep_payload", _post)
    monkeypatch.setattr(run_mod, "get_task_summary", lambda *a, **k: None)
    return payloads


def _run(**kwargs):
    """Invoke run() against an existing task id (no upload, no preflight)."""
    defaults = dict(
        existing_task_id="task-1",
        agent="mini-swe-agent",
        watch=False,
        background=True,
    )
    return run_mod.run(**{**defaults, **kwargs})


def _submitted_config(payloads: list[dict]) -> dict:
    assert len(payloads) == 1, payloads
    configs = payloads[0]["configs"]
    assert len(configs) == 1, configs
    return configs[0]


# ---------------------------------------------------------------------------
# The payload carries the user's spelling, unchanged
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("combo", _CRED_COMBOS)
@pytest.mark.parametrize(
    "model",
    [
        "deepseek-v4-flash",
        "deepseek-v4-pro",
        "glm-5.2",
        "fireworks/deepseek-v4-flash",
        "deepseek/deepseek-v4-flash",
        "claude-sonnet-4-5",
    ],
)
def test_model_is_submitted_verbatim(monkeypatch, combo, model):
    _apply_credentials(monkeypatch, combo)
    payloads = _capture_payloads(monkeypatch)
    _run(model=model)
    assert _submitted_config(payloads)["model"] == model


def test_payload_identical_under_every_credential_combination(monkeypatch):
    """The exact divergence Kyle captured: same command, two local key sets."""
    bodies = []
    for combo in _CRED_COMBOS:
        _apply_credentials(monkeypatch, combo)
        payloads = _capture_payloads(monkeypatch)
        _run(model="deepseek-v4-flash", experiment_id="exp-1")
        bodies.append(json.dumps(payloads[0], sort_keys=True, default=str))
    assert len(set(bodies)) == 1, bodies


def test_client_idempotency_key_identical_under_every_credential_combination(
    monkeypatch,
):
    """A retry from a machine with different keys must reuse the same key.

    ``post_sweep_payload`` derives the ``Idempotency-Key`` header from the
    payload digest, so a credential-dependent payload also produced a
    credential-dependent key.
    """
    captured: list[dict] = []

    class _Response:
        status_code = 200
        text = "{}"

        def json(self):
            return dict(_SWEEP_RESULT)

    class _Client:
        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

        def post(self, _url, json, headers=None):
            captured.append(headers or {})
            return _Response()

    monkeypatch.setattr(api_mod, "get_auth_headers", lambda: {})
    monkeypatch.setattr(api_mod.httpx, "Client", _Client)
    monkeypatch.setattr(run_mod, "get_task_summary", lambda *a, **k: None)

    for combo in _CRED_COMBOS:
        _apply_credentials(monkeypatch, combo)
        _run(model="deepseek-v4-flash", experiment_id="exp-1")

    keys = {headers["Idempotency-Key"] for headers in captured}
    assert len(captured) == len(_CRED_COMBOS)
    assert len(keys) == 1, keys


def test_explicit_provider_flag_is_forwarded(monkeypatch):
    """``--provider`` stays the deliberate pin, resolved server-side."""
    payloads = _capture_payloads(monkeypatch)
    _run(model="deepseek-v4-flash", provider="deepseek")
    config = _submitted_config(payloads)
    assert config["model"] == "deepseek-v4-flash"
    assert config["provider"] == "deepseek"


def test_allow_unknown_model_flag_is_forwarded(monkeypatch):
    payloads = _capture_payloads(monkeypatch)
    _run(model="fireworks/not-a-real-model", allow_unknown_model=True)
    config = _submitted_config(payloads)
    assert config["model"] == "fireworks/not-a-real-model"
    assert config["allow_unknown_model"] is True


def test_locked_agent_mismatch_is_left_to_the_server(monkeypatch):
    """``dsh`` + a Fireworks-only id used to exit(1) locally.

    The server owns that rejection now (it answers 422), so the CLI must send
    the submission rather than pre-judging it from its own alias table.
    """
    payloads = _capture_payloads(monkeypatch)
    _run(agent="dsh", model="deepseek-v4-flash-0731")
    assert _submitted_config(payloads)["model"] == "deepseek-v4-flash-0731"


def test_cli_does_not_resolve_curated_models(monkeypatch):
    """Structural guard against reintroducing client-side resolution."""

    def _forbidden(*args, **kwargs):  # pragma: no cover - only on regression
        raise AssertionError("the CLI must not resolve curated models locally")

    config_mod = importlib.import_module("oddish.config")
    monkeypatch.setattr(config_mod, "auto_resolve_curated_model", _forbidden)
    payloads = _capture_payloads(monkeypatch)
    _run(model="deepseek-v4-flash")
    assert _submitted_config(payloads)["model"] == "deepseek-v4-flash"


# ---------------------------------------------------------------------------
# ``--json`` emits exactly one JSON document on stdout
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("combo", _CRED_COMBOS)
@pytest.mark.parametrize("model", ["deepseek-v4-flash", "glm-5.2", "gpt-5.2"])
def test_json_stdout_is_a_single_document(monkeypatch, combo, model):
    """Kyle's repro, through the real Typer entry point."""
    _apply_credentials(monkeypatch, combo)
    captured: list[dict] = []

    def _post(api_url, payload):
        captured.append(payload)
        return dict(_SWEEP_RESULT)

    monkeypatch.setattr(run_mod, "post_sweep_payload", _post)
    monkeypatch.setattr(run_mod, "get_task_summary", lambda *a, **k: None)

    result = CliRunner().invoke(
        app,
        [
            "run",
            "--task",
            "task-1",
            "-a",
            "mini-swe-agent",
            "-m",
            model,
            "--n-trials",
            "1",
            "--background",
            "--no-watch",
            "--json",
        ],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)  # must not raise
    assert payload["total_trials"] == 1
    assert payload["tasks"][0]["id"] == "task-1"
    assert captured[0]["configs"][0]["model"] == model
