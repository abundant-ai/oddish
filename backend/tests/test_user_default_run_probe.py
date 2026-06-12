"""Unit tests for per-user default-probe helpers (pure, no DB)."""

from __future__ import annotations

from types import SimpleNamespace

from api.routers.tasks import _apply_user_default_run_probe
from oddish.schemas import AgentModelPair, TaskSweepSubmission


def _submission(run_probe: bool) -> TaskSweepSubmission:
    # `configs` is required and a model-validator requires a model for any
    # non-nop/oracle agent, so use a nop config to keep the fixture minimal.
    return TaskSweepSubmission(
        task_id="t1",
        name="t1",
        run_probe=run_probe,
        configs=[AgentModelPair(agent="nop")],
    )


def test_default_on_enables_run_probe():
    sub = _submission(run_probe=False)
    user = SimpleNamespace(settings={"default_run_probe": True})
    _apply_user_default_run_probe(sub, user)
    assert sub.run_probe is True


def test_default_off_leaves_run_probe_false():
    sub = _submission(run_probe=False)
    user = SimpleNamespace(settings={"default_run_probe": False})
    _apply_user_default_run_probe(sub, user)
    assert sub.run_probe is False


def test_default_never_disables_explicit_true():
    sub = _submission(run_probe=True)
    user = SimpleNamespace(settings={})
    _apply_user_default_run_probe(sub, user)
    assert sub.run_probe is True


def test_none_user_is_noop():
    sub = _submission(run_probe=False)
    _apply_user_default_run_probe(sub, None)
    assert sub.run_probe is False


def test_missing_settings_is_noop():
    sub = _submission(run_probe=False)
    user = SimpleNamespace(settings=None)
    _apply_user_default_run_probe(sub, user)
    assert sub.run_probe is False
