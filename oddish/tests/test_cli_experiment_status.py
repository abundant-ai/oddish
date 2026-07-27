"""Regression tests for the ``oddish status --experiment`` rollup.

Focus: an experiment whose every trial is in an errored terminal state used to
render as an internally inconsistent "Trials: 0/1 completed" header over a
"1/1 finished" row with a bare "-" in the Rewards column. That "-" is
indistinguishable from "the grader recorded no reward", so a downstream gate
misread an infrastructure failure as a broken verifier.

These tests pin the fixed behaviour:
  * the header and the per-task row agree on "finished" (terminal), and the
    header no longer says "completed" for the trial count;
  * errored trials are surfaced distinctly (a count in the Rewards cell, plus
    the failure class and retry exhaustion in a detail section) instead of "-";
  * a ran-but-scored-zero task still shows "0/N", staying distinct from both an
    errored task and an absent reward.
"""

from __future__ import annotations

import io
import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from rich.console import Console  # noqa: E402

from oddish.cli import api as api_mod  # noqa: E402


# --- fixtures ----------------------------------------------------------------


def _errored_task(**overrides) -> dict:
    """A task whose only trial hit a harness/infra failure (sandbox build)."""
    task = {
        "id": "emu-task-cb9e",
        "name": "emu-task",
        "experiment_name": "gbapu-fixed2-oracle",
        "status": "completed",  # the task *pipeline* is terminal...
        "total": 1,
        "completed": 0,  # ...but 0 trials ran to SUCCESS
        "failed": 1,  # 1 errored (harness/infra)
        "skipped": 0,
        "progress": "1/1 finished",
        "reward_success": None,
        "reward_total": None,  # no scored trial -> bare "-" before the fix
        "verdict_status": None,
    }
    task.update(overrides)
    return task


_ERRORED_TRIAL = {
    "id": "emu-task-cb9e-30",
    "experiment_id": "05aa25b9",
    "status": "failed",
    "attempts": 6,
    "max_attempts": 6,
    "harbor_stage": "completed",
    "reward": None,
    "error_message": (
        "Sandbox build failed for environment 'task-emu-357q4ytp': "
        "Failed to create sandbox: Failure during waiting for sandbox to "
        "start: Sandbox f9325a39 failed to start with state: "
        'SandboxState.BUILD_FAILED, error reason: process "mvn install" '
        "did not complete successfully: exit code: 1"
    ),
}


def _render(renderable, width: int = 200) -> str:
    buf = io.StringIO()
    Console(file=buf, width=width, color_system=None).print(renderable)
    return buf.getvalue()


# --- counters ----------------------------------------------------------------


def test_summarize_counts_finished_and_errored():
    summary = api_mod._summarize_experiment_tasks([_errored_task()])
    assert summary["total_trials"] == 1
    # "finished" = terminal = completed(SUCCESS) + failed + skipped
    assert summary["finished_trials"] == 1
    assert summary["completed_trials"] == 0  # SUCCESS-only, the old numerator
    assert summary["failed_trials"] == 1
    assert summary["skipped_trials"] == 0


def test_summarize_finished_spans_multiple_tasks():
    tasks = [
        _errored_task(
            total=2, completed=1, failed=1, skipped=0, progress="2/2 finished"
        ),
        _errored_task(
            total=3, completed=0, failed=1, skipped=1, progress="2/3 finished"
        ),
    ]
    summary = api_mod._summarize_experiment_tasks(tasks)
    assert summary["total_trials"] == 5
    assert summary["finished_trials"] == 1 + 1 + 0 + 1 + 1  # 2 + 2
    assert summary["failed_trials"] == 2
    assert summary["skipped_trials"] == 1


# --- error-reason extraction -------------------------------------------------


def test_short_error_reason_prefers_sandbox_state():
    assert (
        api_mod._short_error_reason(_ERRORED_TRIAL["error_message"])
        == "SandboxState.BUILD_FAILED"
    )


def test_short_error_reason_error_class():
    assert (
        api_mod._short_error_reason("boom: TimeoutError: deadline exceeded")
        == "TimeoutError"
    )


def test_short_error_reason_image_build():
    assert (
        api_mod._short_error_reason("Image build for im-abc123 failed at step 3")
        == "ImageBuildFailed"
    )


def test_short_error_reason_fallback_elides_quotes():
    reason = api_mod._short_error_reason("could not reach host 'worker-42.internal'")
    assert reason is not None
    assert "'" not in reason  # environment-specific span elided
    assert reason.startswith("could not reach host")


def test_short_error_reason_fallback_truncates_long_first_line():
    long_line = "connection reset by peer while streaming logs from the remote worker"
    reason = api_mod._short_error_reason(long_line)
    assert reason is not None
    assert reason.endswith("…")
    assert len(reason) <= 60


def test_short_error_reason_none_when_empty():
    assert api_mod._short_error_reason(None) is None
    assert api_mod._short_error_reason("   ") is None


# --- per-task error summary --------------------------------------------------


def test_task_error_summary_with_embedded_trials():
    summary = api_mod._task_error_summary(_errored_task(trials=[_ERRORED_TRIAL]))
    assert summary["errored"] == 1
    assert summary["reason"] == "SandboxState.BUILD_FAILED"
    assert summary["attempts"] == "6/6"  # exhausted retries


def test_task_error_summary_without_embedded_trials_counts_only():
    # The count comes from the task-level ``failed`` counter, so it is always
    # available; reason/attempts need embedded trials and stay None.
    summary = api_mod._task_error_summary(_errored_task())
    assert summary["errored"] == 1
    assert summary["reason"] is None
    assert summary["attempts"] is None


def test_task_error_summary_reports_dominant_reason():
    trials = [
        {**_ERRORED_TRIAL, "id": "t1", "error_message": "TimeoutError: x"},
        {**_ERRORED_TRIAL, "id": "t2"},  # SandboxState.BUILD_FAILED
        {**_ERRORED_TRIAL, "id": "t3"},  # SandboxState.BUILD_FAILED
    ]
    task = _errored_task(total=3, failed=3, progress="3/3 finished", trials=trials)
    assert api_mod._task_error_summary(task)["reason"] == "SandboxState.BUILD_FAILED"


def test_task_error_summary_attempts_only_when_exhausted():
    trial = {**_ERRORED_TRIAL, "attempts": 2, "max_attempts": 6}
    summary = api_mod._task_error_summary(_errored_task(trials=[trial]))
    assert summary["attempts"] is None  # 2/6 is not exhausted


# --- the Rewards cell: errored must not read as "no reward" ------------------


def test_reward_cell_shows_errored_count_not_bare_dash():
    text = _render(
        api_mod._build_experiment_table("EXP", [_errored_task(trials=[_ERRORED_TRIAL])])
    )
    assert "1 errored" in text


def test_reward_cell_zero_reward_is_distinct_from_errored():
    ran_zero = _errored_task(
        id="ran-zero",
        completed=1,
        failed=0,  # it ran; it did not error
        reward_success=0,
        reward_total=1,  # a real grader result of 0
        progress="1/1 finished",
    )
    text = _render(api_mod._build_experiment_table("EXP", [ran_zero]))
    assert "0/1" in text
    assert "errored" not in text


def test_summary_row_renames_failed_trials_to_errored():
    text = _render(
        api_mod._build_experiment_table("EXP", [_errored_task(trials=[_ERRORED_TRIAL])])
    )
    assert "1 errored" in text
    assert "failed trials" not in text


# --- the full-width failure detail section ----------------------------------


def test_error_details_section_lists_class_and_attempts():
    detail = api_mod._build_experiment_error_details(
        [_errored_task(trials=[_ERRORED_TRIAL])]
    )
    assert detail is not None
    text = _render(detail)
    assert "emu-task-cb9e" in text
    assert "SandboxState.BUILD_FAILED" in text  # un-truncated, unlike the cell
    assert "6/6 attempts" in text


def test_error_details_section_none_without_embedded_trials():
    assert api_mod._build_experiment_error_details([_errored_task()]) is None


# --- integration: print_experiment_status with conditional enrichment -------


def _fake_client_factory(cheap_tasks, enriched_tasks, calls):
    class _Resp:
        def __init__(self, payload):
            self.status_code = 200
            self._payload = payload

        def json(self):
            return self._payload

        @property
        def text(self):
            import json

            return json.dumps(self._payload)

    class _Client:
        def __init__(self, *a, **k):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a, **k):
            return False

        def get(self, url, params=None):
            params = params or {}
            calls.append(dict(params))
            # The enrichment fetch is the one that asks for embedded trials.
            if "include_trials" in params:
                return _Resp(enriched_tasks)
            return _Resp(cheap_tasks)

    return _Client


def _run_experiment_status(cheap_tasks, enriched_tasks):
    calls: list[dict] = []
    fake = _fake_client_factory(cheap_tasks, enriched_tasks, calls)
    buf = io.StringIO()
    with (
        patch.object(
            api_mod, "console", Console(file=buf, width=200, color_system=None)
        ),
        patch.object(api_mod.httpx, "Client", fake),
        patch.object(api_mod, "get_auth_headers", return_value={}),
    ):
        ok = api_mod.print_experiment_status("http://localhost", "05aa25b9")
    return ok, buf.getvalue(), calls


def _trials_line(output: str) -> str:
    return next(
        line for line in output.splitlines() if line.strip().startswith("Trials:")
    )


def test_status_experiment_all_errored_is_consistent_and_surfaces_error():
    ok, out, calls = _run_experiment_status(
        [_errored_task()],  # cheap fetch: counts only, trials=None
        [_errored_task(trials=[_ERRORED_TRIAL])],  # enriched: embedded scoped trial
    )
    assert ok is True

    # Header and per-task row now agree on "finished"; the misleading
    # "0/1 completed" header is gone.
    trials_line = _trials_line(out)
    assert "1/1 finished" in trials_line
    assert "completed" not in trials_line
    assert "0/1 completed" not in out
    assert "1/1 finished" in out  # the per-task Progress row, unchanged

    # The errored trial is surfaced distinctly rather than as a bare "-".
    assert "1 errored" in out
    assert "SandboxState.BUILD_FAILED" in out
    assert "6/6 attempts" in out

    # Enrichment happened: exactly one follow-up fetch asked for trials.
    assert sum(1 for c in calls if "include_trials" in c) == 1


def test_status_experiment_healthy_skips_enrichment():
    healthy = _errored_task(
        id="passing-task",
        completed=1,
        failed=0,
        reward_success=1,
        reward_total=1,
        progress="1/1 finished",
    )
    ok, out, calls = _run_experiment_status([healthy], [healthy])
    assert ok is True
    assert "1/1 finished" in _trials_line(out)
    assert "Rewards: 1/1 passed" in out
    # No errors -> no heavier include_trials fetch.
    assert all("include_trials" not in c for c in calls)
