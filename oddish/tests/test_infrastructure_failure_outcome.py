from __future__ import annotations

from contextlib import asynccontextmanager
from types import SimpleNamespace

import harbor.agents.installed.base as harbor_agent_errors
import harbor.trial.errors as harbor_trial_errors
import pytest
from harbor.trial.hooks import TrialEvent
from oddish.cli.api import trial_result_to_import_spec
from oddish.core.harbor_artifacts import (
    SCORE_INVALIDATING_EXCEPTIONS,
    invalidates_score,
)
from oddish.db import TrialStatus
from oddish.worker.local_runner import _verifier_reward_for_result
from oddish.workers.harbor.runner import HarborOutcome
from oddish.workers.queue import trial_handler
from oddish.workers.queue.trial_handler import _store_trial_results
from test_scoreless_trial_no_retry import _patch_session, _trial

# Harbor raises these when the agent's own run ended the trial, so the verifier
# grades work the agent actually did. They must keep their reward.
AGENT_OWNED_ENDINGS = (
    "AgentTimeoutError",
    "AgentSafetyRefusalError",
    "ContextWindowExceededError",
    "OutputTokenExceededError",
)


def _outcome(**overrides):
    defaults = {
        "reward": 0.0,
        "error": None,
        "exit_code": 0,
        "duration_sec": 1.0,
        "job_result_path": None,
        "job_dir": None,
    }
    defaults.update(overrides)
    return HarborOutcome(**defaults)


async def _store(monkeypatch, trial, outcome):
    _patch_session(monkeypatch, trial)
    await _store_trial_results(
        trial_id=trial.id,
        outcome=outcome,
        trial_s3_key=None,
        execution_error=None,
        trial_attempt=trial.attempts,
    )


def _claude_trial(**overrides):
    return _trial(
        agent="claude-code",
        model="anthropic/claude-opus-4-7",
        attempts=1,
        max_attempts=3,
        **overrides,
    )


@pytest.mark.parametrize(
    ("exception_type", "expected"),
    [
        ("ApiClientError", True),
        ("ApiUsageLimitError", True),
        ("AgentAuthenticationError", True),
        ("ModelNotFoundError", True),
        ("ApiProviderResourceNotFoundError", True),
        ("ApiOverloadedError", True),
        ("NetworkConnectionError", True),
        ("AgentTimeoutError", False),
        ("AgentSafetyRefusalError", False),
        ("ContextWindowExceededError", False),
        ("OutputTokenExceededError", False),
        ("VerifierTimeoutError", False),
        ("AddTestsDirError", False),
        (None, False),
    ],
)
def test_invalidates_score(exception_type, expected):
    assert invalidates_score(exception_type) is expected


def _harbor_exception_class(name: str) -> type | None:
    for module in (harbor_agent_errors, harbor_trial_errors):
        candidate = getattr(module, name, None)
        if isinstance(candidate, type):
            return candidate
    return None


def test_every_score_invalidating_provider_name_is_a_harbor_exception():
    # The set is matched by name against ``exception_info.exception_type``.
    # A name Harbor never raises would silently protect nothing.
    for name in SCORE_INVALIDATING_EXCEPTIONS:
        assert _harbor_exception_class(name) is not None, name


def test_agent_owned_endings_do_not_invalidate_scores():
    for name in AGENT_OWNED_ENDINGS:
        assert _harbor_exception_class(name) is not None, name
        assert name not in SCORE_INVALIDATING_EXCEPTIONS


@pytest.mark.asyncio
async def test_spend_cap_rejection_is_not_a_score(monkeypatch):
    # The reported incident: the first model call was rejected with HTTP 400,
    # the agent never touched the environment, and the verifier graded that
    # untouched environment as a 0.
    trial = _claude_trial()
    await _store(
        monkeypatch,
        trial,
        _outcome(
            reward=0.0,
            error="ApiClientError: 400 organization spend cap reached",
            exception_type="ApiClientError",
            http_status=400,
        ),
    )

    assert trial.reward is None
    assert trial.status == TrialStatus.FAILED
    assert trial.error_message == "ApiClientError: 400 organization spend cap reached"
    assert trial.finished_at is not None
    harbor_exception = trial.result["harbor_exception"]
    assert harbor_exception["exception_type"] == "ApiClientError"
    assert harbor_exception["http_status"] == 400


@pytest.mark.asyncio
async def test_unusable_model_id_is_not_a_score(monkeypatch):
    trial = _claude_trial()
    await _store(
        monkeypatch,
        trial,
        _outcome(
            reward=0.0,
            error="ModelNotFoundError: 404 unknown model",
            exception_type="ModelNotFoundError",
            http_status=404,
        ),
    )

    assert trial.reward is None
    assert trial.status == TrialStatus.FAILED
    assert trial.error_message == "ModelNotFoundError: 404 unknown model"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "reward, total_steps, output_tokens", [(0.0, 1, 0), (0.4, 8, 1200)]
)
async def test_transient_provider_failure_discards_score_even_after_partial_work(
    monkeypatch, reward, total_steps, output_tokens
):
    # Harbor's retry policy still owns the retry decision. Dropping the reward
    # only puts the trial back on the path it takes when no reward is reported.
    trial = _claude_trial()
    await _store(
        monkeypatch,
        trial,
        _outcome(
            reward=reward,
            error="ApiOverloadedError: 529 overloaded",
            exception_type="ApiOverloadedError",
            http_status=529,
            total_steps=total_steps,
            output_tokens=output_tokens,
        ),
    )

    assert trial.reward is None
    assert trial.status == TrialStatus.RETRYING
    assert trial.finished_at is None
    assert trial.total_steps == total_steps
    assert trial.output_tokens == output_tokens


@pytest.mark.asyncio
@pytest.mark.parametrize("exception_type", AGENT_OWNED_ENDINGS)
async def test_agent_owned_ending_keeps_its_verifier_reward(
    monkeypatch, exception_type
):
    trial = _claude_trial()
    await _store(
        monkeypatch,
        trial,
        _outcome(
            reward=0.0,
            error=f"{exception_type}: the agent's run ended",
            exception_type=exception_type,
        ),
    )

    assert trial.reward == 0.0
    assert trial.status == TrialStatus.SUCCESS


@pytest.mark.asyncio
async def test_clean_run_keeps_a_real_zero(monkeypatch):
    trial = _claude_trial()
    await _store(monkeypatch, trial, _outcome(reward=0.0))

    assert trial.reward == 0.0
    assert trial.status == TrialStatus.SUCCESS
    assert trial.error_message is None


@pytest.mark.asyncio
async def test_clean_run_keeps_a_passing_reward(monkeypatch):
    trial = _claude_trial()
    await _store(monkeypatch, trial, _outcome(reward=1.0))

    assert trial.reward == 1.0
    assert trial.status == TrialStatus.SUCCESS


@pytest.mark.asyncio
async def test_provider_rejection_drops_even_a_passing_reward(monkeypatch):
    # The policy discards rewards based on the recorded exception, including
    # a passing reward. It does not use the reward to infer completed work.
    trial = _claude_trial()
    await _store(
        monkeypatch,
        trial,
        _outcome(
            reward=1.0,
            error="ApiUsageLimitError: account limit exhausted",
            exception_type="ApiUsageLimitError",
        ),
    )

    assert trial.reward is None
    assert trial.status == TrialStatus.FAILED


def _imported_trial_result(exception_type: str | None, reward: float = 0.0):
    exception_info = None
    if exception_type is not None:
        exception_info = SimpleNamespace(
            exception_type=exception_type,
            exception_message=f"{exception_type}: provider rejected the request",
            http_status=400,
            request_id=None,
            session_id=None,
            retry_after_seconds=None,
        )
    return SimpleNamespace(
        id="trial-id",
        agent_info=SimpleNamespace(name="claude-code", model_info=None),
        config=SimpleNamespace(
            agent=SimpleNamespace(model_name="anthropic/claude-opus-4-7")
        ),
        verifier_result=SimpleNamespace(rewards={"reward": reward}),
        exception_info=exception_info,
        agent_result=None,
        environment_setup=None,
        agent_setup=None,
        agent_execution=None,
        verifier=None,
        started_at=None,
        finished_at=None,
    )


def test_imported_provider_rejection_is_not_a_score():
    spec = trial_result_to_import_spec(_imported_trial_result("ApiClientError"))

    assert spec["reward"] is None
    assert spec["status"] == "failed"
    assert spec["error_message"] == "ApiClientError: provider rejected the request"


def test_imported_agent_timeout_keeps_its_score():
    spec = trial_result_to_import_spec(_imported_trial_result("AgentTimeoutError"))

    assert spec["reward"] == 0.0
    assert spec["status"] == "success"


def test_imported_clean_run_keeps_a_real_zero():
    spec = trial_result_to_import_spec(_imported_trial_result(None))

    assert spec["reward"] == 0.0
    assert spec["status"] == "success"


def _end_event(exception_type: str | None, reward: float | None = 0.0):
    verifier_result = (
        SimpleNamespace(rewards={"reward": reward}) if reward is not None else None
    )
    exception_info = None
    if exception_type is not None:
        exception_info = SimpleNamespace(
            exception_type=exception_type,
            exception_message=f"{exception_type}: provider rejected the request",
        )
    return SimpleNamespace(
        event=TrialEvent.END,
        timestamp=None,
        environment=None,
        environment_external_id=None,
        environment_provider=None,
        result=SimpleNamespace(
            verifier_result=verifier_result,
            exception_info=exception_info,
        ),
    )


async def _handle_end(monkeypatch, trial, hook_event):
    @asynccontextmanager
    async def fake_trial_session(
        _trial_id, *, allow_missing=False, with_for_update=False
    ):
        yield SimpleNamespace(execute=lambda *_a, **_kw: None), trial

    monkeypatch.setattr(trial_handler, "_trial_session", fake_trial_session)
    await trial_handler._handle_harbor_event(hook_event, trial_id=trial.id)


@pytest.mark.asyncio
async def test_end_hook_does_not_stamp_success_on_a_provider_rejection(monkeypatch):
    # The hook stamps a terminal row before settlement runs. A worker that dies
    # in between must not leave SUCCESS/0.0 as the trial's final word. The row
    # stays running instead, so settlement -- or the stale-heartbeat sweep --
    # owns the retry-or-fail decision.
    trial = _claude_trial()
    await _handle_end(monkeypatch, trial, _end_event("ApiClientError"))

    assert trial.reward is None
    assert trial.status == TrialStatus.RUNNING
    assert trial.finished_at is None
    assert trial.error_message == "ApiClientError: provider rejected the request"


@pytest.mark.asyncio
async def test_end_hook_keeps_a_real_zero(monkeypatch):
    trial = _claude_trial()
    await _handle_end(monkeypatch, trial, _end_event(None))

    assert trial.reward == 0.0
    assert trial.status == TrialStatus.SUCCESS


def _local_result(exception_type: str | None, reward: float | str | None = 0.0):
    exception_info = None
    if exception_type is not None:
        exception_info = SimpleNamespace(
            exception_type=exception_type,
            exception_message=f"{exception_type}: provider rejected the request",
        )
    verifier_result = (
        SimpleNamespace(rewards={"reward": reward}) if reward is not None else None
    )
    return SimpleNamespace(
        exception_info=exception_info,
        verifier_result=verifier_result,
    )


@pytest.mark.parametrize("reward", [0.0, 1.0])
def test_local_runner_drops_a_reward_from_a_provider_rejection(reward):
    assert _verifier_reward_for_result(_local_result("ApiClientError", reward)) is None


def test_local_runner_keeps_an_agent_owned_ending_reward():
    assert _verifier_reward_for_result(_local_result("AgentTimeoutError", 0.0)) == 0.0


def test_local_runner_keeps_a_real_zero():
    assert _verifier_reward_for_result(_local_result(None, 0.0)) == 0.0


@pytest.mark.parametrize(
    "result",
    [
        None,
        _local_result(None, None),
        _local_result(None, "not-a-number"),
    ],
)
def test_local_runner_reports_no_reward_when_the_verifier_did_not(result):
    assert _verifier_reward_for_result(result) is None
