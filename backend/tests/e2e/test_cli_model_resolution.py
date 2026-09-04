"""End-to-end: the server resolves curated model ids, not the client machine.

Runs the real ``oddish`` CLI as a subprocess against a real uvicorn server and a
real Postgres, varying only the *client's* provider credentials between two
otherwise identical submissions. The server process is session-scoped and its
environment never changes, so any difference in what lands in ``trials.model``
came from the client -- which is the bug these tests pin shut.

Before the fix the CLI called ``auto_resolve_curated_model`` itself and pinned an
explicit provider prefix read from the local environment: a laptop holding
``DEEPSEEK_API_KEY`` sent ``deepseek/deepseek-v4-flash`` and the same laptop
without it sent ``fireworks/deepseek-v4-flash-0731``. Those are different
payloads, so they also produced different client idempotency keys and a second
stored trial.
"""

from __future__ import annotations

import json

import pytest
from sqlalchemy import func, select

from .conftest import DB_URL, E2E_ENABLED, cli

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(
        not (E2E_ENABLED and DB_URL),
        reason="e2e opt-in: set ODDISH_E2E=1 and ODDISH_DATABASE_URL",
    ),
]

_BARE_MODEL = "deepseek-v4-flash"
_AGENT = "mini-swe-agent"
_PROVIDER_ENV = ("FIREWORKS_API_KEY", "DEEPSEEK_API_KEY")


def _set_client_credentials(monkeypatch, *present: str) -> None:
    """Shape the CLI subprocess's provider keys; the server is unaffected."""
    for name in _PROVIDER_ENV:
        if name in present:
            monkeypatch.setenv(name, f"sk-e2e-{name.lower()}")
        else:
            monkeypatch.delenv(name, raising=False)


def _submit(live_server, seeded, monkeypatch, *credentials: str):
    _set_client_credentials(monkeypatch, *credentials)
    proc = cli(
        live_server,
        seeded["api_key"],
        "run",
        "--task",
        seeded["task_id"],
        "--agent",
        _AGENT,
        "--model",
        _BARE_MODEL,
        "--n-trials",
        "1",
        "--background",
        "--json",
    )
    assert proc.returncode == 0, f"stdout:\n{proc.stdout}\nstderr:\n{proc.stderr}"
    # The --json contract: stdout is exactly one JSON document. The CLI used to
    # print "Model: auto-selected ..." ahead of it for precisely this model.
    payload = json.loads(proc.stdout)
    return proc, payload


async def _trial_models(task_id: str) -> list[str]:
    from oddish.db import TrialModel, get_session

    async with get_session() as session:
        rows = await session.scalars(
            select(TrialModel.model).where(TrialModel.task_id == task_id)
        )
        return sorted(rows)


async def _trial_count(task_id: str) -> int:
    from oddish.db import TrialModel, get_session

    async with get_session() as session:
        return await session.scalar(
            select(func.count())
            .select_from(TrialModel)
            .where(TrialModel.task_id == task_id)
        )


async def test_server_canonicalizes_a_bare_curated_id(
    live_server, seeded, monkeypatch
):
    """The stored model is the canonical Fireworks id, chosen server-side."""
    _submit(live_server, seeded, monkeypatch)

    assert await _trial_models(seeded["task_id"]) == [
        "fireworks/deepseek-v4-flash-0731"
    ]


async def test_stored_model_ignores_the_clients_provider_keys(
    live_server, seeded, monkeypatch
):
    """Kyle's repro: same command, two local key sets, one stored model.

    A DeepSeek key on the client used to redirect the run to DeepSeek even
    though the client's keys say nothing about the server's credentials.
    """
    _submit(live_server, seeded, monkeypatch, "DEEPSEEK_API_KEY")
    after_deepseek = await _trial_models(seeded["task_id"])

    _submit(live_server, seeded, monkeypatch, "FIREWORKS_API_KEY")
    after_fireworks = await _trial_models(seeded["task_id"])

    assert after_deepseek == ["fireworks/deepseek-v4-flash-0731"]
    assert after_fireworks == after_deepseek


async def test_repeat_submission_replays_instead_of_adding_a_trial(
    live_server, seeded, monkeypatch
):
    """The client idempotency key is stable across differing local credentials.

    ``compute_sweep_idempotency_key`` digests the payload, so a credential-
    dependent payload also produced a credential-dependent key. Two runs then
    described two different (agent, model) pairs and left two trials behind.
    """
    _submit(live_server, seeded, monkeypatch, "DEEPSEEK_API_KEY")
    _submit(live_server, seeded, monkeypatch, "FIREWORKS_API_KEY")
    _submit(live_server, seeded, monkeypatch)

    assert await _trial_count(seeded["task_id"]) == 1


async def test_json_stdout_parses_for_a_bare_curated_id(
    live_server, seeded, monkeypatch
):
    _, payload = _submit(live_server, seeded, monkeypatch, "DEEPSEEK_API_KEY")

    assert payload["total_trials"] == 1
    assert payload["tasks"][0]["id"] == seeded["task_id"]


async def test_explicit_provider_still_pins(live_server, seeded, monkeypatch):
    """``--provider`` remains the deliberate override, applied server-side."""
    _set_client_credentials(monkeypatch)
    proc = cli(
        live_server,
        seeded["api_key"],
        "run",
        "--task",
        seeded["task_id"],
        "--agent",
        _AGENT,
        "--model",
        _BARE_MODEL,
        "--provider",
        "deepseek",
        "--n-trials",
        "1",
        "--background",
        "--json",
    )
    assert proc.returncode == 0, f"stdout:\n{proc.stdout}\nstderr:\n{proc.stderr}"

    models = await _trial_models(seeded["task_id"])
    assert len(models) == 1
    assert models[0].startswith("deepseek/")
