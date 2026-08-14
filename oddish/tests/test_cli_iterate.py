"""Closed-loop contract for ``oddish iterate``."""

from __future__ import annotations

import importlib
import json
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
import typer
from typer.testing import CliRunner

from oddish.cli import app
from oddish.core.idempotency import compute_sweep_idempotency_key
from oddish.schemas import (
    TaskReviewResponse,
    TaskVersionManifestEntry,
    TaskVersionManifestResponse,
    TaskVersionResponse,
)

iterate_module = importlib.import_module("oddish.cli.iterate")


def _trial(
    trial_id: str,
    role: str,
    *,
    fingerprint: str | None = None,
    reward: float = 1,
    classification: str | None = None,
    status: str = "success",
) -> dict[str, Any]:
    analysis = None
    analysis_status = None
    if classification is not None:
        analysis_status = "success"
        analysis = {
            "classification": classification,
            "subtype": "Wrong Approach",
            "evidence": "Exact stored evidence.",
            "root_cause": "The task is sound.",
            "recommendation": "N/A",
            "action_items": [],
            "exploitation": [],
        }
    return {
        "id": trial_id,
        "role": role,
        "experiment_id": "exp-iterate",
        "agent": "codex" if role == "model" else role,
        "model": "openai/gpt-5.6" if role == "model" else None,
        "config_fingerprint": fingerprint or f"sha256:{trial_id}",
        "environment": "docker",
        "harbor_sha": "a" * 40,
        "status": status,
        "reward": reward,
        "cost_usd": 0.42 if role == "model" else 0,
        "duration_seconds": 123.5,
        "included_in_result_run": role == "model",
        "result_run_analysis_fingerprint": (
            "sha256:analysis" if role == "model" else None
        ),
        "analysis_matches_result_run": True if role == "model" else None,
        "analysis_status": analysis_status,
        "analysis": analysis,
    }


def _finding(finding_id: str) -> dict[str, Any]:
    return {
        "id": finding_id,
        "source": "post_trial",
        "problem_type": "mismatch",
        "dimension": "verifier",
        "file": "tests/test_task.py",
        "line_start": 10,
        "line_end": 12,
        "title": f"Stored title {finding_id}",
        "detail": f"Stored detail {finding_id}",
        "recommendation": f"Stored recommendation {finding_id}",
        "tier": "must_fix",
        "links_to": None,
        "exploited": False,
        "exploit_evidence": None,
        "causal": True,
        "from_pre_trial": False,
        "trial_ids": ["task-1-2"],
        "experiment_ids": ["exp-iterate"],
    }


def _review(
    *,
    version: int = 1,
    experiment_id: str = "exp-iterate",
    fingerprints: tuple[str, ...] = ("sha256:pilot",),
    counts: tuple[int, ...] | None = None,
    completed: bool = True,
    baseline_outcome: str = "valid",
    verdict: str = "accept",
    classification: str = "GOOD_FAILURE",
    reward: float = 0,
    findings: tuple[str, ...] = (),
) -> TaskReviewResponse:
    counts = counts or tuple(1 for _ in fingerprints)
    model_trials: list[dict[str, Any]] = []
    index = 2
    for fingerprint, count in zip(fingerprints, counts):
        for _ in range(count):
            row = _trial(
                f"task-1-{index}",
                "model",
                fingerprint=fingerprint,
                reward=reward,
                classification=classification if completed else None,
                status="success" if completed else "queued",
            )
            row["experiment_id"] = experiment_id
            model_trials.append(row)
            index += 1

    baseline_trials = [
        _trial("task-1-0", "nop", reward=0),
        _trial("task-1-1", "oracle", reward=1),
    ]
    for row in baseline_trials:
        row["experiment_id"] = experiment_id

    finding_rows = [_finding(finding_id) for finding_id in findings]
    run = {
        "id": f"qa-run-v{version}",
        "disposition": "published" if completed else None,
        "task_version_id": f"task-1-v{version}",
        "worker_job_id": f"qa-job-v{version}",
        "input_trial_count": len(model_trials),
        "input_set_sha256": "sha256:inputs",
        "input_analysis_changed_count": 0,
        "pre_trial_block_id": f"pre-block-v{version}",
        "verdict_block_id": f"verdict-block-v{version}" if completed else None,
        "started_at": "2026-08-14T01:00:00Z",
        "finished_at": "2026-08-14T01:05:00Z" if completed else None,
    }
    classifications = {
        "GOOD_FAILURE": 0,
        "BAD_FAILURE": 0,
        "GOOD_SUCCESS": 0,
        "BAD_SUCCESS": 0,
        "HARNESS_ERROR": 0,
    }
    if completed:
        classifications[classification] = len(model_trials)
    return TaskReviewResponse.model_validate(
        {
            "schema_version": 1,
            "task": {
                "id": "task-1",
                "name": "sample-task",
                "version": version,
                "version_id": f"task-1-v{version}",
                "content_hash": f"content-v{version}",
            },
            "scope": {
                "experiment_id": experiment_id,
                "tiers": ["must_fix", "should_fix", "optional"],
                "same_version_across_experiments": False,
            },
            "qa": {
                "status": "success" if completed else "queued",
                "result_run": run if completed else None,
                "active_run": None if completed else run,
                "is_task_published_run": completed,
                "legacy_unscoped_verdict_available": False,
                "input_analysis_changed_after_run": False,
            },
            "baselines": {
                "outcome": baseline_outcome,
                "nop": {
                    "expected_reward": 0,
                    "valid": baseline_outcome == "valid",
                    "trial_count": 1 if baseline_outcome == "valid" else 0,
                    "unexpected_count": 0,
                },
                "oracle": {
                    "expected_reward": 1,
                    "valid": True,
                    "trial_count": 1,
                    "unexpected_count": 0,
                },
            },
            "verdict": (
                {
                    "verdict": verdict,
                    "is_good": verdict == "accept",
                    "confidence": "high",
                    "primary_issue": None if verdict == "accept" else "Task issue.",
                    "reasoning": "Stored reasoning.",
                    "recommendations": [],
                    "task_problem_count": 0 if verdict == "accept" else 1,
                    "agent_problem_count": 0,
                    "success_count": 0,
                    "harness_error_count": 0,
                }
                if completed
                else None
            ),
            "finding_counts": {
                "unfiltered_total": len(finding_rows),
                "filtered_total": len(finding_rows),
                "must_fix": len(finding_rows),
                "should_fix": 0,
                "optional": 0,
            },
            "findings": finding_rows,
            "findings_page": {"has_more": False, "next_cursor": None},
            "trial_counts": {
                "eligible": len(model_trials),
                "analyzed": len(model_trials) if completed else 0,
                "unanalyzed": 0 if completed else len(model_trials),
                "classifications": classifications,
            },
            "trials": [*baseline_trials, *model_trials],
            "trials_page": {"has_more": False, "next_cursor": None},
        }
    )


def _version(version: int = 1) -> TaskVersionResponse:
    return TaskVersionResponse(
        id=f"task-1-v{version}",
        task_id="task-1",
        version=version,
        task_path=f"tasks/task-1/v{version}.tar.gz",
        content_hash=f"content-v{version}",
        created_at=datetime(2026, 8, 14, tzinfo=UTC),
    )


def _manifest(version: int = 1, status: str = "ready") -> TaskVersionManifestResponse:
    return TaskVersionManifestResponse(
        task_id="task-1",
        version_id=f"task-1-v{version}",
        version=version,
        content_hash=f"content-v{version}",
        status=status,
        files=[],
    )


def _existing(review: TaskReviewResponse | None = None, version: int = 1):
    selected_review = review or _review(version=version)
    item = _version(version)
    return iterate_module._ExistingTask(
        id="task-1",
        name="sample-task",
        versions=(item,),
        latest_version=item,
        latest_review=selected_review,
        latest_manifest=_manifest(version),
    )


def _prior(review: TaskReviewResponse | None = None, version: int = 1):
    selected_review = review or _review(version=version)
    return iterate_module._PriorIteration(
        version_id=f"task-1-v{version}",
        version=version,
        experiment_id="exp-iterate",
        experiment_name=f"iterate-sample-task-v{version}",
        review=selected_review,
    )


def _patch_local(monkeypatch, tmp_path: Path, *, pinned: str | None = None) -> None:
    (tmp_path / "task.toml").write_text("[environment]\n", encoding="utf-8")
    monkeypatch.setattr(iterate_module, "run_checks", lambda _paths: [])
    monkeypatch.setattr(
        iterate_module, "validate_task_timeout_config", lambda _path: None
    )
    monkeypatch.setattr(
        iterate_module, "validate_no_git_lfs_pointers", lambda _path: None
    )
    monkeypatch.setattr(
        iterate_module,
        "HarborTaskConfig",
        SimpleNamespace(
            model_validate_toml=lambda _text: SimpleNamespace(
                environment=SimpleNamespace(docker_image=pinned)
            )
        ),
    )
    monkeypatch.setattr(
        iterate_module, "compute_task_content_hash", lambda _path: "local-content"
    )
    monkeypatch.setattr(iterate_module, "hash_local_task_files", lambda _path: {})


def _patch_submission(
    monkeypatch,
    *,
    review: TaskReviewResponse,
    upload: dict[str, Any] | None = None,
    payloads: list[dict[str, Any]] | None = None,
) -> None:
    upload_payload = upload or {
        "task_id": "task-1",
        "version": review.task.version,
        "version_id": review.task.version_id,
        "existing_task": False,
        "content_unchanged": False,
        "content_hash": review.task.content_hash,
    }
    monkeypatch.setattr(
        iterate_module,
        "upload_tasks_with_progress",
        lambda *args, **kwargs: [upload_payload],
    )

    def _post(_api_url, payload):
        if payloads is not None:
            payloads.append(payload)
        return {
            "id": "task-1",
            "experiment_id": "exp-iterate",
            "experiment_name": f"iterate-sample-task-v{review.task.version}",
            "new_trial_ids": ["task-1-0", "task-1-1", "task-1-2"],
            "trials_count": 3,
        }

    monkeypatch.setattr(iterate_module, "post_sweep_payload", _post)
    monkeypatch.setattr(
        iterate_module,
        "watch_task",
        lambda *args, **kwargs: {"id": "task-1", "status": "completed"},
    )
    monkeypatch.setattr(
        iterate_module, "fetch_complete_review", lambda *args, **kwargs: review
    )


def test_first_run_without_model_fails_before_upload(monkeypatch, tmp_path) -> None:
    _patch_local(monkeypatch, tmp_path)
    monkeypatch.setattr(iterate_module, "_resolve_existing_task", lambda *args: None)
    uploaded = False

    def _upload(*args, **kwargs):
        nonlocal uploaded
        uploaded = True
        return []

    monkeypatch.setattr(iterate_module, "upload_tasks_with_progress", _upload)

    with pytest.raises(ValueError, match="--agent AGENT --model MODEL"):
        iterate_module._run_iteration(
            task_path=tmp_path,
            api_url="https://api.example.test",
            agent=None,
            model=None,
            same_sweep=False,
            environment=None,
            force_build=False,
            reuse_pinned_image=False,
            registry_auth=None,
            json_output=True,
        )

    assert uploaded is False


def test_existing_resolution_uses_numerically_highest_immutable_version(
    monkeypatch,
) -> None:
    identity = _review(version=1)
    latest = _review(version=3)
    fetched_versions: list[int] = []
    manifest_versions: list[int] = []
    monkeypatch.setattr(
        iterate_module, "get_task_review", lambda *args, **kwargs: identity
    )
    monkeypatch.setattr(
        iterate_module,
        "_get_task_versions",
        lambda *args: [_version(3), _version(1), _version(2)],
    )

    def _fetch(*args, **kwargs):
        fetched_versions.append(kwargs["version"])
        return latest

    def _wait(_api_url, _task_id, version):
        manifest_versions.append(version)
        return _manifest(version)

    monkeypatch.setattr(iterate_module, "fetch_complete_review", _fetch)
    monkeypatch.setattr(iterate_module, "_wait_for_manifest", _wait)

    existing = iterate_module._resolve_existing_task(
        "https://api.example.test", "sample-task"
    )

    assert existing is not None
    assert existing.latest_version.version == 3
    assert fetched_versions == [3]
    assert manifest_versions == [3]


def test_only_review_404_means_first_upload(monkeypatch) -> None:
    response = httpx.Response(
        404,
        request=httpx.Request("GET", "https://api.example.test/tasks/new-task/review"),
    )

    def _missing(*args, **kwargs):
        raise httpx.HTTPStatusError(
            "missing", request=response.request, response=response
        )

    monkeypatch.setattr(iterate_module, "get_task_review", _missing)
    monkeypatch.setattr(
        iterate_module,
        "_get_task_versions",
        lambda *args: pytest.fail("version lookup must not follow a 404"),
    )

    assert (
        iterate_module._resolve_existing_task("https://api.example.test", "new-task")
        is None
    )


def test_explicit_first_run_submits_one_nop_oracle_and_model(
    monkeypatch, tmp_path
) -> None:
    _patch_local(monkeypatch, tmp_path)
    monkeypatch.setattr(iterate_module, "_resolve_existing_task", lambda *args: None)
    after = _review()
    payloads: list[dict[str, Any]] = []
    _patch_submission(monkeypatch, review=after, payloads=payloads)

    result = iterate_module._run_iteration(
        task_path=tmp_path,
        api_url="https://api.example.test",
        agent="codex",
        model="openai/gpt-5.6",
        same_sweep=False,
        environment=None,
        force_build=False,
        reuse_pinned_image=False,
        registry_auth=None,
        json_output=True,
    )

    assert result == 0
    assert [(row["agent"], row.get("model")) for row in payloads[0]["configs"]] == [
        ("nop", None),
        ("oracle", None),
        ("codex", "openai/gpt-5.6"),
    ]
    assert payloads[0]["run_analysis"] is True
    assert payloads[0]["gate_baselines"] is True


def test_subsequent_run_reuses_one_prior_pilot(monkeypatch, tmp_path) -> None:
    _patch_local(monkeypatch, tmp_path)
    prior_review = _review(completed=False)
    existing = _existing(prior_review)
    prior = _prior(prior_review)
    monkeypatch.setattr(
        iterate_module, "_resolve_existing_task", lambda *args: existing
    )
    monkeypatch.setattr(iterate_module, "_find_latest_iteration", lambda *args: prior)
    monkeypatch.setattr(
        iterate_module,
        "get_trial_detail",
        lambda *args, **kwargs: {
            "agent": "codex",
            "model": "openai/gpt-5.6",
            "environment": "docker",
            "harbor_config": {
                "source": "https://github.com/example/harbor.git",
                "ref": "main",
            },
        },
    )
    after = _review()
    payloads: list[dict[str, Any]] = []
    _patch_submission(
        monkeypatch,
        review=after,
        upload={
            "task_id": "task-1",
            "version": 1,
            "version_id": "task-1-v1",
            "existing_task": True,
            "content_unchanged": True,
            "content_hash": "content-v1",
        },
        payloads=payloads,
    )

    assert (
        iterate_module._run_iteration(
            task_path=tmp_path,
            api_url="https://api.example.test",
            agent=None,
            model=None,
            same_sweep=False,
            environment=None,
            force_build=False,
            reuse_pinned_image=False,
            registry_auth=None,
            json_output=True,
        )
        == 0
    )
    assert payloads[0]["configs"][2]["agent"] == "codex"
    assert payloads[0]["configs"][2]["n_trials"] == 1
    assert payloads[0]["experiment_id"] == "iterate-sample-task-v1"


def test_ambiguous_prior_configs_fail_without_detail_or_upload(
    monkeypatch, tmp_path
) -> None:
    _patch_local(monkeypatch, tmp_path)
    prior = _prior(_review(fingerprints=("sha256:a", "sha256:b")))
    existing = _existing(prior.review)
    monkeypatch.setattr(
        iterate_module, "_resolve_existing_task", lambda *args: existing
    )
    monkeypatch.setattr(iterate_module, "_find_latest_iteration", lambda *args: prior)
    monkeypatch.setattr(
        iterate_module,
        "get_trial_detail",
        lambda *args, **kwargs: pytest.fail("detail must not be fetched"),
    )
    monkeypatch.setattr(
        iterate_module,
        "upload_tasks_with_progress",
        lambda *args, **kwargs: pytest.fail("upload must not run"),
    )

    with pytest.raises(ValueError, match="--same-sweep"):
        iterate_module._run_iteration(
            task_path=tmp_path,
            api_url="https://api.example.test",
            agent=None,
            model=None,
            same_sweep=False,
            environment=None,
            force_build=False,
            reuse_pinned_image=False,
            registry_auth=None,
            json_output=True,
        )


def test_same_sweep_fetches_one_detail_per_fingerprint_and_strips_secrets(
    monkeypatch,
) -> None:
    prior = _prior(
        _review(
            fingerprints=("sha256:a", "sha256:b"),
            counts=(2, 1),
        )
    )
    calls: list[str] = []

    def _detail(_api_url, trial_id, *, json_output):
        calls.append(trial_id)
        return {
            "agent": "codex",
            "model": "openai/gpt-5.6",
            "environment": "docker",
            "harbor_config": {
                "source": "https://github.com/example/harbor.git",
                "ref": "main",
                "resolved_sha": "b" * 40,
                "agent_config": {
                    "env": {"SAFE_FLAG": "yes", "API_TOKEN": "supersecret"},
                    "kwargs": {"mode": "careful", "password": "also-secret"},
                },
            },
        }

    monkeypatch.setattr(iterate_module, "get_trial_detail", _detail)
    selected = iterate_module._reuse_configs(
        "https://api.example.test", prior, same_sweep=True
    )

    assert len(calls) == 2
    assert [row["n_trials"] for row in selected.configs] == [2, 1]
    serialized = json.dumps(
        {"configs": selected.configs, "harbor": selected.harbor_config}
    )
    assert "SAFE_FLAG" in serialized
    assert "supersecret" not in serialized
    assert "also-secret" not in serialized
    assert "resolved_sha" not in serialized


def test_registry_parser_is_reused_and_errors_redact_credentials(
    monkeypatch, tmp_path
) -> None:
    monkeypatch.setenv("ODDISH_API_KEY", "ok_test")
    monkeypatch.setattr(
        iterate_module, "resolve_local_task_paths", lambda **kwargs: [tmp_path]
    )
    monkeypatch.setattr(iterate_module, "is_task_dir", lambda _path: True)
    captured: dict[str, Any] = {}

    def _run(**kwargs):
        captured.update(kwargs)
        raise RuntimeError("network response contained private-token-value")

    monkeypatch.setattr(iterate_module, "_run_iteration", _run)
    result = CliRunner().invoke(
        app,
        [
            "iterate",
            str(tmp_path),
            "--agent",
            "codex",
            "--model",
            "openai/gpt-5.6",
            "--registry-login",
            "username=alice,token=private-token-value,registry=ghcr.io",
            "--api",
            "https://api.example.test",
        ],
    )

    assert result.exit_code == 1
    assert captured["registry_auth"][0]["token"] == "private-token-value"
    assert "private-token-value" not in result.output
    assert "REDACTED" in result.output


def test_dataset_root_is_rejected_even_when_it_contains_one_task(
    monkeypatch, tmp_path
) -> None:
    monkeypatch.setenv("ODDISH_API_KEY", "ok_test")
    child = tmp_path / "only-task"
    child.mkdir()
    monkeypatch.setattr(
        iterate_module, "resolve_local_task_paths", lambda **kwargs: [child]
    )
    monkeypatch.setattr(iterate_module, "is_task_dir", lambda path: path == child)
    monkeypatch.setattr(
        iterate_module,
        "_run_iteration",
        lambda **kwargs: pytest.fail("dataset root must not start iteration"),
    )

    result = CliRunner().invoke(
        app,
        [
            "iterate",
            str(tmp_path),
            "--agent",
            "codex",
            "--model",
            "openai/gpt-5.6",
            "--api",
            "https://api.example.test",
        ],
    )

    assert result.exit_code == 1
    assert "exactly one local task directory" in result.output


def test_completed_unchanged_iteration_submits_nothing(monkeypatch, tmp_path) -> None:
    _patch_local(monkeypatch, tmp_path)
    completed = _review()
    existing = _existing(completed)
    prior = _prior(completed)
    monkeypatch.setattr(
        iterate_module, "_resolve_existing_task", lambda *args: existing
    )
    monkeypatch.setattr(iterate_module, "_find_latest_iteration", lambda *args: prior)
    monkeypatch.setattr(
        iterate_module,
        "upload_tasks_with_progress",
        lambda *args, **kwargs: [
            {
                "task_id": "task-1",
                "version": 1,
                "version_id": "task-1-v1",
                "existing_task": True,
                "content_unchanged": True,
                "content_hash": "content-v1",
            }
        ],
    )
    monkeypatch.setattr(
        iterate_module,
        "post_sweep_payload",
        lambda *args, **kwargs: pytest.fail("completed iteration must not submit"),
    )
    monkeypatch.setattr(
        iterate_module,
        "watch_task",
        lambda *args, **kwargs: pytest.fail("completed iteration must not watch"),
    )

    assert (
        iterate_module._run_iteration(
            task_path=tmp_path,
            api_url="https://api.example.test",
            agent="codex",
            model="openai/gpt-5.6",
            same_sweep=False,
            environment=None,
            force_build=False,
            reuse_pinned_image=False,
            registry_auth=None,
            json_output=True,
        )
        == 0
    )


def test_absent_unchanged_iteration_resumes_exact_version_and_name(
    monkeypatch, tmp_path
) -> None:
    _patch_local(monkeypatch, tmp_path)
    existing = _existing(_review(version=3), version=3)
    monkeypatch.setattr(
        iterate_module, "_resolve_existing_task", lambda *args: existing
    )
    monkeypatch.setattr(iterate_module, "_find_latest_iteration", lambda *args: None)
    monkeypatch.setattr(
        iterate_module, "_iteration_for_version", lambda *args, **kwargs: None
    )
    payloads: list[dict[str, Any]] = []
    _patch_submission(
        monkeypatch,
        review=_review(version=3),
        upload={
            "task_id": "task-1",
            "version": 3,
            "version_id": "task-1-v3",
            "existing_task": True,
            "content_unchanged": True,
            "content_hash": "content-v3",
        },
        payloads=payloads,
    )

    assert (
        iterate_module._run_iteration(
            task_path=tmp_path,
            api_url="https://api.example.test",
            agent="codex",
            model="openai/gpt-5.6",
            same_sweep=False,
            environment=None,
            force_build=False,
            reuse_pinned_image=False,
            registry_auth=None,
            json_output=True,
        )
        == 0
    )
    assert payloads[0]["task_id"] == "task-1"
    assert payloads[0]["append_to_task"] is True
    assert payloads[0]["experiment_id"] == "iterate-sample-task-v3"


def test_pinned_image_fails_before_upload_even_with_force_build(
    monkeypatch, tmp_path
) -> None:
    _patch_local(monkeypatch, tmp_path, pinned="ghcr.io/acme/task:v1")
    monkeypatch.setattr(
        iterate_module,
        "upload_tasks_with_progress",
        lambda *args, **kwargs: pytest.fail("pinned image must fail before upload"),
    )

    with pytest.raises(typer.Exit) as excinfo:
        iterate_module._run_iteration(
            task_path=tmp_path,
            api_url="https://api.example.test",
            agent="codex",
            model="openai/gpt-5.6",
            same_sweep=False,
            environment=None,
            force_build=True,
            reuse_pinned_image=False,
            registry_auth=None,
            json_output=True,
        )
    assert excinfo.value.exit_code == 1


def test_manifest_pending_waits_only_to_short_deadline(monkeypatch) -> None:
    clock = [0.0]
    calls = 0

    def _get(*args):
        nonlocal calls
        calls += 1
        return _manifest(status="pending")

    monkeypatch.setattr(iterate_module, "get_task_version_manifest", _get)
    monkeypatch.setattr(iterate_module, "_MANIFEST_WAIT_SECONDS", 1.0)
    monkeypatch.setattr(iterate_module, "_MANIFEST_POLL_SECONDS", 0.25)
    monkeypatch.setattr(iterate_module.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(
        iterate_module.time,
        "sleep",
        lambda seconds: clock.__setitem__(0, clock[0] + seconds),
    )

    result = iterate_module._wait_for_manifest("https://api.example.test", "task-1", 1)

    assert result.status == "pending"
    assert clock[0] == 1.0
    assert calls == 5


def test_manifest_diff_reports_exact_archive_change_kinds() -> None:
    previous = _manifest()
    previous.files = [
        TaskVersionManifestEntry(path="same.txt", size=4, sha256="a" * 64),
        TaskVersionManifestEntry(path="changed.txt", size=6, sha256="b" * 64),
        TaskVersionManifestEntry(path="deleted.txt", size=7, sha256="c" * 64),
        TaskVersionManifestEntry(
            path="large.bin",
            size=100,
            sha256=None,
            skipped=True,
            skip_reason="too_large",
        ),
    ]
    local = {
        "same.txt": TaskVersionManifestEntry(path="same.txt", size=4, sha256="a" * 64),
        "changed.txt": TaskVersionManifestEntry(
            path="changed.txt", size=8, sha256="d" * 64
        ),
        "added.txt": TaskVersionManifestEntry(
            path="added.txt", size=3, sha256="e" * 64
        ),
        "large.bin": TaskVersionManifestEntry(
            path="large.bin", size=100, sha256="f" * 64
        ),
    }

    assert iterate_module._diff_manifest(previous, local) == {
        "status": "ready",
        "added": ["added.txt"],
        "modified": ["changed.txt"],
        "deleted": ["deleted.txt"],
        "unchanged": ["same.txt"],
        "unavailable": ["large.bin"],
    }


def test_replay_builds_same_experiment_and_idempotency_key(
    monkeypatch, tmp_path
) -> None:
    _patch_local(monkeypatch, tmp_path)
    existing = _existing(_review(completed=False))
    monkeypatch.setattr(
        iterate_module, "_resolve_existing_task", lambda *args: existing
    )
    monkeypatch.setattr(iterate_module, "_find_latest_iteration", lambda *args: None)
    monkeypatch.setattr(
        iterate_module, "_iteration_for_version", lambda *args, **kwargs: None
    )
    payloads: list[dict[str, Any]] = []
    _patch_submission(
        monkeypatch,
        review=_review(),
        upload={
            "task_id": "task-1",
            "version": 1,
            "version_id": "task-1-v1",
            "existing_task": True,
            "content_unchanged": True,
            "content_hash": "content-v1",
        },
        payloads=payloads,
    )

    for _ in range(2):
        assert (
            iterate_module._run_iteration(
                task_path=tmp_path,
                api_url="https://api.example.test",
                agent="codex",
                model="openai/gpt-5.6",
                same_sweep=False,
                environment=None,
                force_build=False,
                reuse_pinned_image=False,
                registry_auth=None,
                json_output=True,
            )
            == 0
        )

    assert payloads[0]["experiment_id"] == payloads[1]["experiment_id"]
    assert compute_sweep_idempotency_key(payloads[0]) == compute_sweep_idempotency_key(
        payloads[1]
    )


def test_baseline_rejection_is_exit_two_before_task_failure() -> None:
    review = _review(baseline_outcome="faulty", verdict="reject")
    assert iterate_module._result_exit_code(review, {"status": "failed"}) == 2


def test_worker_failure_and_task_rejection_have_distinct_exit_codes() -> None:
    worker_failure = _review()
    worker_failure.trials[-1].status = "failed"
    rejection = _review(verdict="reject")

    assert iterate_module._result_exit_code(worker_failure, {"status": "failed"}) == 1
    assert iterate_module._result_exit_code(rejection, {"status": "completed"}) == 2


def test_json_contains_provenance_evidence_timing_and_id_comparison() -> None:
    before = _review(findings=("finding-remaining", "finding-before"))
    after = _review(findings=("finding-remaining", "finding-after"))
    document = iterate_module._json_document(
        review=after,
        prior_review=before,
        experiment_id="exp-iterate",
        experiment_name="iterate-sample-task-v1",
        content_hash="content-v1",
        content_unchanged=False,
        submitted=True,
        changed_files={
            "status": "ready",
            "added": ["new.py"],
            "modified": [],
            "deleted": [],
            "unchanged": ["task.toml"],
            "unavailable": [],
        },
    )

    encoded = json.dumps(document)
    assert document["task"]["id"] == "task-1"
    assert document["task"]["version_id"] == "task-1-v1"
    assert document["experiment"]["id"] == "exp-iterate"
    assert document["qa"]["result_run"]["id"] == "qa-run-v1"
    assert document["qa"]["result_run"]["worker_job_id"] == "qa-job-v1"
    assert document["qa"]["result_run"]["pre_trial_block_id"] == "pre-block-v1"
    assert document["qa"]["result_run"]["verdict_block_id"] == "verdict-block-v1"
    assert document["trials"][-1]["harbor_sha"] == "a" * 40
    assert document["trials"][-1]["environment"] == "docker"
    assert document["trials"][-1]["cost_usd"] == 0.42
    assert document["trials"][-1]["duration_seconds"] == 123.5
    assert document["baselines"]["nop"]["expected_reward"] == 0
    assert document["comparison"]["findings"] == {
        "remaining": ["finding-remaining"],
        "introduced": ["finding-after"],
        "not_observed_after": ["finding-before"],
    }
    assert "fixed" not in encoded.lower()


def test_reward_zero_and_good_failure_remain_separate_in_human_output(capsys) -> None:
    review = _review(reward=0, classification="GOOD_FAILURE")
    iterate_module._render_human(
        review=review,
        prior_review=None,
        experiment_id="exp-iterate",
        experiment_name="iterate-sample-task-v1",
        content_hash="content-v1",
        content_unchanged=False,
        submitted=True,
        changed_files={
            "status": "ready",
            "added": [],
            "modified": [],
            "deleted": [],
            "unchanged": [],
            "unavailable": [],
        },
    )

    output = capsys.readouterr().out
    assert "Verifier ✗ reward 0" in output
    assert "QA good failure" in output
    assert "not_observed_after" in output.lower()
