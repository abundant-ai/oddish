"""Client-side idempotency key for ``oddish`` task-sweep submission.

``submit_sweep`` stamps each ``POST /tasks/sweep`` with an ``Idempotency-Key``
header derived from a stable digest of the submission payload so that a retried
identical submission carries the same key (and a changed spec a different one).
"""

from __future__ import annotations

from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from oddish.cli import api as cli_api  # noqa: E402
from oddish.cli.api import submit_sweep  # noqa: E402
from oddish.core.idempotency import compute_sweep_idempotency_key  # noqa: E402


def _payload(task_id: str = "task-1", configs=None, experiment_id="exp-1") -> dict:
    return {
        "task_id": task_id,
        "experiment_id": experiment_id,
        "configs": configs or [{"agent": "codex", "model": "gpt-5", "n_trials": 2}],
        "priority": "low",
    }


def test_identical_payloads_produce_same_key() -> None:
    assert compute_sweep_idempotency_key(_payload()) == compute_sweep_idempotency_key(
        _payload()
    )


def test_key_is_order_independent() -> None:
    # Dict insertion order must not change the digest.
    a = {"task_id": "t", "configs": [1], "experiment_id": "e"}
    b = {"experiment_id": "e", "configs": [1], "task_id": "t"}
    assert compute_sweep_idempotency_key(a) == compute_sweep_idempotency_key(b)


def test_different_spec_produces_different_key() -> None:
    base = compute_sweep_idempotency_key(_payload())
    # Different task, experiment, and config each flip the key.
    assert compute_sweep_idempotency_key(_payload(task_id="task-2")) != base
    assert compute_sweep_idempotency_key(_payload(experiment_id="exp-2")) != base
    assert (
        compute_sweep_idempotency_key(
            _payload(configs=[{"agent": "codex", "model": "gpt-5", "n_trials": 3}])
        )
        != base
    )


def test_submit_sweep_sends_idempotency_key_header(monkeypatch) -> None:
    captured: list[dict] = []

    class _Response:
        status_code = 200
        text = "{}"

        def json(self):
            return {"id": "task-1", "trials_count": 1}

    class _Client:
        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

        def post(self, _url, json, headers=None):
            captured.append({"json": json, "headers": headers or {}})
            return _Response()

    monkeypatch.setattr(cli_api, "get_auth_headers", lambda: {})
    monkeypatch.setattr(cli_api.httpx, "Client", _Client)

    base_kwargs = dict(
        api_url="https://oddish.example",
        task_id="task-1",
        configs=[{"agent": "codex", "model": "gpt-5", "n_trials": 1}],
        environment=None,
        user=None,
        priority="low",
        experiment_id="exp-1",
    )

    submit_sweep(**base_kwargs)
    submit_sweep(**base_kwargs)  # identical retry
    submit_sweep(**{**base_kwargs, "task_id": "task-2"})  # different spec

    first = captured[0]["headers"]["Idempotency-Key"]
    retry = captured[1]["headers"]["Idempotency-Key"]
    other = captured[2]["headers"]["Idempotency-Key"]

    assert first and isinstance(first, str)
    assert first == retry  # identical re-submit -> same key
    assert first != other  # different spec -> different key
    # The header matches a direct digest of the posted body.
    assert first == compute_sweep_idempotency_key(captured[0]["json"])
