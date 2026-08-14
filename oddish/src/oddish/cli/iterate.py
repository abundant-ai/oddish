"""Closed-loop task iteration: upload, gate, execute, analyze, and compare."""

from __future__ import annotations

import copy
import json
import os
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Any, Optional
from urllib.parse import quote

import httpx
import typer
from rich.console import Console
from rich.markup import escape

from harbor.models.environment_type import EnvironmentType
from harbor.models.task.config import TaskConfig as HarborTaskConfig

from oddish.cli import api as cli_api
from oddish.cli.api import (
    build_sweep_payload,
    compute_task_content_hash,
    get_task_review,
    get_task_version_manifest,
    hash_local_task_files,
    is_task_dir,
    post_sweep_payload,
    resolve_local_task_paths,
    upload_tasks_with_progress,
    validate_no_git_lfs_pointers,
    watch_task,
)
from oddish.cli.config import (
    error_console,
    get_api_url,
    get_auth_headers,
    print_json,
    require_api_key,
)
from oddish.cli.detail import get_trial_detail
from oddish.cli.preflight import gate_preflight
from oddish.cli.review import fetch_complete_review, render_review
from oddish.config import is_nop_oracle_agent
from oddish.db import TaskQaRunDisposition, TrialStatus, VerdictStatus
from oddish.preflight.runner import run_checks
from oddish.registry_auth import parse_registry_login
from oddish.schemas import (
    AgentModelPair,
    HarborConfig,
    TaskOpenResponse,
    TaskReviewResponse,
    TaskReviewTrial,
    TaskVersionManifestEntry,
    TaskVersionManifestResponse,
    TaskVersionResponse,
)
from oddish.task_timeouts import (
    TaskTimeoutValidationError,
    validate_task_timeout_config,
)

console = Console()

_EXPERIMENT_NAME_LIMIT = 255
_MANIFEST_WAIT_SECONDS = 5.0
_MANIFEST_POLL_SECONDS = 0.25
_TERMINAL_TRIAL_STATUSES = {
    TrialStatus.SUCCESS,
    TrialStatus.FAILED,
    TrialStatus.SKIPPED,
}
_SECRET_KEY_PARTS = (
    "secret",
    "password",
    "token",
    "api_key",
    "apikey",
    "credential",
    "authorization",
    "access_key",
    "private_key",
)


@dataclass(frozen=True)
class _ExistingTask:
    id: str
    name: str
    versions: tuple[TaskVersionResponse, ...]
    latest_version: TaskVersionResponse
    latest_review: TaskReviewResponse
    latest_manifest: TaskVersionManifestResponse


@dataclass(frozen=True)
class _PriorIteration:
    version_id: str
    version: int
    experiment_id: str
    experiment_name: str
    review: TaskReviewResponse


@dataclass(frozen=True)
class _SelectedConfigs:
    configs: tuple[dict[str, Any], ...]
    harbor_config: dict[str, Any] | None


def _get_task_versions(api_url: str, task_id: str) -> list[TaskVersionResponse]:
    with httpx.Client(timeout=30.0, headers=get_auth_headers(api_url)) as client:
        response = client.get(
            f"{api_url.rstrip('/')}/tasks/{quote(task_id, safe='')}/versions"
        )
    response.raise_for_status()
    return [TaskVersionResponse.model_validate(item) for item in response.json()]


def _get_task_open(
    api_url: str,
    task_id: str,
    *,
    version_id: str,
) -> TaskOpenResponse:
    with httpx.Client(timeout=30.0, headers=get_auth_headers(api_url)) as client:
        response = client.get(
            f"{api_url.rstrip('/')}/tasks/{quote(task_id, safe='')}/open",
            params={"version_id": version_id},
        )
    response.raise_for_status()
    return TaskOpenResponse.model_validate(response.json())


def _wait_for_manifest(
    api_url: str,
    task_id: str,
    version: int,
) -> TaskVersionManifestResponse:
    """Wait briefly for expansion, without turning a legacy miss into a hang."""

    manifest = get_task_version_manifest(api_url, task_id, version)
    if manifest.status != "pending":
        return manifest

    deadline = time.monotonic() + _MANIFEST_WAIT_SECONDS
    while manifest.status == "pending":
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return manifest
        time.sleep(min(_MANIFEST_POLL_SECONDS, remaining))
        manifest = get_task_version_manifest(api_url, task_id, version)
    return manifest


def _resolve_existing_task(api_url: str, task_name: str) -> _ExistingTask | None:
    """Resolve an exact live task name, treating only an authoritative 404 as new."""

    try:
        identity = get_task_review(
            api_url,
            task_name,
            finding_limit=0,
            trial_limit=0,
        )
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code == 404:
            return None
        raise

    versions = sorted(
        _get_task_versions(api_url, identity.task.id),
        key=lambda item: item.version,
    )
    if not versions:
        raise ValueError(
            f"Task {identity.task.id} exists but has no immutable task versions"
        )
    latest = versions[-1]
    review = fetch_complete_review(
        api_url,
        identity.task.id,
        version=latest.version,
        experiment_id=None,
        tiers=None,
    )
    manifest = _wait_for_manifest(api_url, identity.task.id, latest.version)
    return _ExistingTask(
        id=identity.task.id,
        name=identity.task.name,
        versions=tuple(versions),
        latest_version=latest,
        latest_review=review,
        latest_manifest=manifest,
    )


def _experiment_name(task_name: str, version: int) -> str:
    """Build the stable iteration name while preserving its version suffix."""

    slug = re.sub(r"[^A-Za-z0-9._-]+", "-", task_name.strip())
    slug = re.sub(r"-+", "-", slug).strip("-._") or "task"
    prefix = "iterate-"
    suffix = f"-v{version}"
    available = _EXPERIMENT_NAME_LIMIT - len(prefix) - len(suffix)
    return f"{prefix}{slug[:available].rstrip('-._') or 'task'}{suffix}"


def _iteration_for_version(
    api_url: str,
    task_id: str,
    task_name: str,
    *,
    version_id: str,
    version: int,
) -> _PriorIteration | None:
    expected_name = _experiment_name(task_name, version)
    opened = _get_task_open(api_url, task_id, version_id=version_id)
    selected = opened.selected_version
    if selected is None or selected.id != version_id:
        raise ValueError(
            f"Task open response did not select requested version {version_id}"
        )
    experiment = next(
        (item for item in selected.experiments if item.name == expected_name),
        None,
    )
    if experiment is None:
        return None
    review = fetch_complete_review(
        api_url,
        task_id,
        version=version,
        experiment_id=experiment.id,
        tiers=None,
    )
    return _PriorIteration(
        version_id=version_id,
        version=version,
        experiment_id=experiment.id,
        experiment_name=experiment.name,
        review=review,
    )


def _find_latest_iteration(
    api_url: str,
    existing: _ExistingTask,
) -> _PriorIteration | None:
    for version in reversed(existing.versions):
        prior = _iteration_for_version(
            api_url,
            existing.id,
            existing.name,
            version_id=version.id,
            version=version.version,
        )
        if prior is not None:
            return prior
    return None


def _strip_secret_keys(value: Any) -> Any:
    """Copy reusable config while refusing secret-bearing key/value pairs."""

    if isinstance(value, dict):
        return {
            key: _strip_secret_keys(child)
            for key, child in value.items()
            if not any(part in str(key).lower() for part in _SECRET_KEY_PARTS)
        }
    if isinstance(value, list):
        return [_strip_secret_keys(child) for child in value]
    return copy.deepcopy(value)


def _validated_config(payload: dict[str, Any], *, n_trials: int) -> dict[str, Any]:
    validated = AgentModelPair.model_validate({**payload, "n_trials": n_trials})
    result = validated.model_dump(
        mode="json",
        exclude_none=True,
        exclude_defaults=True,
    )
    result["n_trials"] = n_trials
    return result


def _explicit_configs(agent: str, model: str) -> _SelectedConfigs:
    return _SelectedConfigs(
        configs=(_validated_config({"agent": agent, "model": model}, n_trials=1),),
        harbor_config=None,
    )


def _reusable_harbor_config(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    allowed = set(HarborConfig.model_fields) - {"resolved_sha", "variant_id"}
    payload = {
        key: child for key, child in _strip_secret_keys(value).items() if key in allowed
    }
    if not payload:
        return None
    validated = HarborConfig.model_validate(payload)
    dumped = validated.model_dump(
        mode="json",
        exclude_none=True,
        exclude_defaults=True,
    )
    return dumped or None


def _reuse_configs(
    api_url: str,
    prior: _PriorIteration,
    *,
    same_sweep: bool,
    task_path: Path | None = None,
) -> _SelectedConfigs:
    groups: dict[str, list[TaskReviewTrial]] = {}
    for trial in prior.review.trials:
        if trial.role == "model":
            groups.setdefault(trial.config_fingerprint, []).append(trial)

    if not groups:
        raise ValueError(
            "The prior iteration has no reusable model config. Resolve it with: "
            "oddish iterate PATH --agent AGENT --model MODEL"
        )
    if len(groups) > 1 and not same_sweep:
        rendered_path = str(task_path) if task_path is not None else "PATH"
        raise ValueError(
            "The prior iteration has multiple model configs. Resolve it with either "
            f"`oddish iterate {rendered_path} --same-sweep` or "
            f"`oddish iterate {rendered_path} --agent AGENT --model MODEL`."
        )

    selected_fingerprints = sorted(groups) if same_sweep else [next(iter(groups))]
    configs: list[dict[str, Any]] = []
    common_harbor: dict[str, Any] | None = None
    common_harbor_encoding: str | None = None
    for fingerprint in selected_fingerprints:
        rows = sorted(groups[fingerprint], key=lambda row: row.id)
        detail = get_trial_detail(api_url, rows[0].id, json_output=True)
        if detail is None:
            raise ValueError(f"Could not recover stored config for trial {rows[0].id}")

        raw_harbor = _strip_secret_keys(detail.get("harbor_config") or {})
        agent_config = (
            raw_harbor.pop("agent_config", None)
            if isinstance(raw_harbor, dict)
            else None
        )
        config: dict[str, Any] = {
            "agent": detail.get("agent") or rows[0].agent,
            "model": detail.get("model", rows[0].model),
        }
        environment = detail.get("environment") or rows[0].environment
        if environment is not None:
            config["environment"] = environment
        if isinstance(agent_config, dict) and agent_config:
            config["agent_config"] = agent_config
        configs.append(
            _validated_config(
                config,
                n_trials=len(rows) if same_sweep else 1,
            )
        )

        harbor = _reusable_harbor_config(raw_harbor)
        encoding = json.dumps(harbor, sort_keys=True, separators=(",", ":"))
        if common_harbor_encoding is None:
            common_harbor = harbor
            common_harbor_encoding = encoding
        elif encoding != common_harbor_encoding:
            raise ValueError(
                "The prior iteration mixes Harbor execution configs that cannot "
                "be replayed as one atomic sweep; choose an explicit --agent/--model."
            )

    return _SelectedConfigs(configs=tuple(configs), harbor_config=common_harbor)


def _iteration_complete(review: TaskReviewResponse) -> bool:
    run = review.qa.result_run
    if (
        review.qa.status != VerdictStatus.SUCCESS
        or run is None
        or run.disposition != TaskQaRunDisposition.PUBLISHED
        or run.finished_at is None
        or not review.trials
    ):
        return False
    return all(trial.status in _TERMINAL_TRIAL_STATUSES for trial in review.trials)


def _diff_manifest(
    previous: TaskVersionManifestResponse | None,
    local: dict[str, TaskVersionManifestEntry],
) -> dict[str, Any]:
    if previous is None:
        return {
            "status": "first_version",
            "added": sorted(local),
            "modified": [],
            "deleted": [],
            "unchanged": [],
            "unavailable": [],
        }
    if previous.status != "ready":
        return {
            "status": "unavailable",
            "added": [],
            "modified": [],
            "deleted": [],
            "unchanged": [],
            "unavailable": [],
        }

    stored = {entry.path: entry for entry in previous.files}
    added = sorted(set(local) - set(stored))
    deleted = sorted(set(stored) - set(local))
    modified: list[str] = []
    unchanged: list[str] = []
    unavailable: list[str] = []
    for path in sorted(set(local) & set(stored)):
        before = stored[path]
        after = local[path]
        if before.skipped or before.sha256 is None:
            unavailable.append(path)
        elif before.sha256 == after.sha256 and before.size == after.size:
            unchanged.append(path)
        else:
            modified.append(path)
    return {
        "status": "ready",
        "added": added,
        "modified": modified,
        "deleted": deleted,
        "unchanged": unchanged,
        "unavailable": unavailable,
    }


def _finding_comparison(
    before: TaskReviewResponse | None,
    after: TaskReviewResponse,
) -> dict[str, list[str]]:
    before_ids = {item.id for item in before.findings} if before is not None else set()
    after_ids = {item.id for item in after.findings}
    return {
        "remaining": sorted(before_ids & after_ids),
        "introduced": sorted(after_ids - before_ids),
        "not_observed_after": sorted(before_ids - after_ids),
    }


def _classification(trial: TaskReviewTrial) -> str | None:
    if trial.analysis is not None:
        value = trial.analysis.classification
        return value.value if hasattr(value, "value") else str(value)
    if trial.analysis_status is not None:
        return trial.analysis_status.value
    return None


def _outcomes(review: TaskReviewResponse | None) -> dict[str, list[dict[str, Any]]]:
    result: dict[str, list[dict[str, Any]]] = {}
    if review is None:
        return result
    for trial in sorted(review.trials, key=lambda row: row.id):
        if trial.role != "model":
            continue
        result.setdefault(trial.config_fingerprint, []).append(
            {
                "trial_id": trial.id,
                "agent": trial.agent,
                "model": trial.model,
                "reward": trial.reward,
                "classification": _classification(trial),
            }
        )
    return result


def _outcome_comparison(
    before: TaskReviewResponse | None,
    after: TaskReviewResponse,
) -> list[dict[str, Any]]:
    before_groups = _outcomes(before)
    after_groups = _outcomes(after)
    return [
        {
            "config_fingerprint": fingerprint,
            "before": before_groups.get(fingerprint, []),
            "after": after_groups.get(fingerprint, []),
        }
        for fingerprint in sorted(set(before_groups) | set(after_groups))
    ]


def _json_document(
    *,
    review: TaskReviewResponse,
    prior_review: TaskReviewResponse | None,
    experiment_id: str,
    experiment_name: str,
    content_hash: str,
    content_unchanged: bool,
    submitted: bool,
    changed_files: dict[str, Any],
) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "task": review.task.model_dump(mode="json"),
        "experiment": {"id": experiment_id, "name": experiment_name},
        "task_content_hash": content_hash,
        "content_unchanged": content_unchanged,
        "operation": "resume" if content_unchanged else "new_version",
        "submitted": submitted,
        "changed_files": changed_files,
        "qa": review.qa.model_dump(mode="json"),
        "baselines": review.baselines.model_dump(mode="json"),
        "verdict": review.verdict.model_dump(mode="json") if review.verdict else None,
        "finding_counts": review.finding_counts.model_dump(mode="json"),
        "findings": [item.model_dump(mode="json") for item in review.findings],
        "trial_counts": review.trial_counts.model_dump(mode="json"),
        "trials": [item.model_dump(mode="json") for item in review.trials],
        "comparison": {
            "findings": _finding_comparison(prior_review, review),
            "model_outcomes": _outcome_comparison(prior_review, review),
        },
    }


def _render_changed_files(changed: dict[str, Any]) -> None:
    status = changed["status"]
    if status == "unavailable":
        console.print("[bold]Changed files[/bold]  unavailable for previous version")
        return
    if status == "first_version":
        console.print("[bold]Changed files[/bold]  first stored version")
    for key in ("added", "modified", "deleted", "unavailable"):
        paths = changed[key]
        if paths:
            console.print(
                f"  [bold]{key.replace('_', ' ').title()}[/bold]  " + ", ".join(paths)
            )
    if not any(changed[key] for key in ("added", "modified", "deleted", "unavailable")):
        console.print("[bold]Changed files[/bold]  none")


def _render_comparison(
    before: TaskReviewResponse | None,
    after: TaskReviewResponse,
) -> None:
    comparison = _finding_comparison(before, after)
    console.print("\n[bold]FINDING COMPARISON[/bold]")
    for key in ("remaining", "introduced", "not_observed_after"):
        ids = comparison[key]
        label = key.upper()
        console.print(f"[bold]{label}[/bold]  {', '.join(ids) if ids else '-'}")


def _render_human(
    *,
    review: TaskReviewResponse,
    prior_review: TaskReviewResponse | None,
    experiment_id: str,
    experiment_name: str,
    content_hash: str,
    content_unchanged: bool,
    submitted: bool,
    changed_files: dict[str, Any],
) -> None:
    operation = (
        "resume/evaluate identical stored version"
        if content_unchanged
        else "new immutable version"
    )
    console.print()
    console.print(f"[bold]Operation[/bold]      {operation}")
    console.print(
        f"[bold]Experiment[/bold]     {escape(experiment_name)} ({escape(experiment_id)})"
    )
    console.print(f"[bold]Task content hash[/bold]  {escape(content_hash)}")
    console.print(
        f"[bold]Submitted[/bold]      {'yes' if submitted else 'no; reused completed iteration'}"
    )
    _render_changed_files(changed_files)
    console.print()
    render_review(
        review,
        title="TASK ITERATION",
        read_only_notice=False,
    )
    _render_comparison(prior_review, review)


def _result_exit_code(review: TaskReviewResponse, final_task: dict | None) -> int:
    # A broken/missing baseline is task evidence, not an infrastructure error,
    # even if the gate made the task itself terminal-failed.
    if review.baselines.outcome == "faulty":
        return 2
    if any(
        trial.role == "model" and trial.status == TrialStatus.FAILED
        for trial in review.trials
    ):
        return 1
    if review.qa.status == VerdictStatus.FAILED:
        return 1
    if final_task is not None and final_task.get("status") == "failed":
        return 1
    if review.verdict is not None and not review.verdict.is_good:
        return 2
    if review.qa.result_run is None:
        return 1
    return 0


def _run_iteration(
    *,
    task_path: Path,
    api_url: str,
    agent: str | None,
    model: str | None,
    same_sweep: bool,
    environment: EnvironmentType | None,
    force_build: bool,
    reuse_pinned_image: bool,
    registry_auth: list[dict[str, Any]] | None,
    json_output: bool,
) -> int:
    findings = run_checks([task_path])
    gate_preflight(findings, force=False, json_output=False)
    try:
        validate_task_timeout_config(task_path)
    except TaskTimeoutValidationError as exc:
        error_console.print(f"[red]Invalid task timeout config:[/red] {exc}")
        raise typer.Exit(1) from exc
    validate_no_git_lfs_pointers(task_path)

    try:
        task_config = HarborTaskConfig.model_validate_toml(
            (task_path / "task.toml").read_text()
        )
    except Exception as exc:
        error_console.print(f"[red]Invalid task.toml:[/red] {exc}")
        raise typer.Exit(1) from exc
    pinned_image = task_config.environment.docker_image
    if pinned_image and not reuse_pinned_image:
        error_console.print(
            "[red]This task selects a pinned docker_image.[/red] "
            "Local source edits may not exist in that image. Re-run with "
            "[bold]--reuse-pinned-image[/bold] only after verifying the image "
            "contains the intended code. --force-build does not replace it."
        )
        raise typer.Exit(1)

    local_content_hash = compute_task_content_hash(task_path)
    local_files = hash_local_task_files(task_path)
    existing = _resolve_existing_task(api_url, task_path.name)

    explicit = agent is not None and model is not None
    prior_iteration: _PriorIteration | None = None
    if existing is not None:
        prior_iteration = _find_latest_iteration(api_url, existing)

    if explicit:
        selection = _explicit_configs(agent, model)
    else:
        if prior_iteration is None:
            raise ValueError(
                "No prior iteration has a reusable model config. Resolve it with: "
                f"oddish iterate {task_path} --agent AGENT --model MODEL"
            )
        selection = _reuse_configs(
            api_url,
            prior_iteration,
            same_sweep=same_sweep,
            task_path=task_path,
        )

    upload = upload_tasks_with_progress(
        api_url,
        [task_path],
        register=False,
        quiet=False,
        json_output=json_output,
        progress_label="Uploading iteration",
    )[0]
    task_id = str(upload["task_id"])
    version = int(upload["version"])
    content_hash = str(upload.get("content_hash") or local_content_hash)
    content_unchanged = bool(upload.get("content_unchanged"))
    authoritative_task_name = existing.name if existing is not None else task_path.name
    experiment_name = _experiment_name(authoritative_task_name, version)

    # upload_task normalizes descriptive task.toml typography before archiving;
    # refresh the raw archive evidence after that existing upload behavior.
    local_files_after_upload = hash_local_task_files(task_path)
    if local_files_after_upload != local_files:
        local_files = local_files_after_upload
    changed_files = _diff_manifest(
        existing.latest_manifest if existing is not None else None,
        local_files,
    )

    exact_iteration: _PriorIteration | None = None
    if existing is not None and content_unchanged:
        exact_iteration = next(
            (
                item
                for item in [prior_iteration]
                if item is not None
                and item.version == version
                and item.experiment_name == experiment_name
            ),
            None,
        )
    if exact_iteration is None and bool(upload.get("existing_task")):
        try:
            exact_iteration = _iteration_for_version(
                api_url,
                task_id,
                authoritative_task_name,
                version_id=str(upload.get("version_id") or f"{task_id}-v{version}"),
                version=version,
            )
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code != 404:
                raise

    submitted = False
    final_task: dict | None = None
    if exact_iteration is not None and _iteration_complete(exact_iteration.review):
        review = exact_iteration.review
        experiment_id = exact_iteration.experiment_id
    else:
        configs = [
            {"agent": "nop", "model": None, "n_trials": 1},
            {"agent": "oracle", "model": None, "n_trials": 1},
            *copy.deepcopy(list(selection.configs)),
        ]
        payload = build_sweep_payload(
            task_id=task_id,
            configs=configs,
            environment=environment,
            user=None,
            priority="low",
            experiment_id=experiment_name,
            run_analysis=True,
            gate_baselines=True,
            force_build=force_build,
            append_to_task=bool(upload.get("existing_task")),
            content_hash=content_hash,
            harbor_config=copy.deepcopy(selection.harbor_config),
            registry_auth=registry_auth,
        )
        result = post_sweep_payload(api_url, payload)
        submitted = True
        experiment_id = str(
            result.get("experiment_id")
            or (exact_iteration.experiment_id if exact_iteration else "")
        )
        if not experiment_id:
            raise ValueError("Sweep response did not identify its experiment")
        trial_ids = result.get("new_trial_ids") or None
        if json_output:
            with cli_api.console.capture():
                final_task = watch_task(
                    api_url,
                    task_id,
                    experiment_id=experiment_id,
                    trial_ids=trial_ids,
                    wait_for_qa=True,
                )
        else:
            final_task = watch_task(
                api_url,
                task_id,
                experiment_id=experiment_id,
                trial_ids=trial_ids,
                wait_for_qa=True,
            )
        if final_task is None:
            raise RuntimeError("Task watch ended without a final task response")
        review = fetch_complete_review(
            api_url,
            task_id,
            version=version,
            experiment_id=experiment_id,
            tiers=None,
        )

    prior_review = prior_iteration.review if prior_iteration is not None else None
    document = _json_document(
        review=review,
        prior_review=prior_review,
        experiment_id=experiment_id,
        experiment_name=experiment_name,
        content_hash=content_hash,
        content_unchanged=content_unchanged,
        submitted=submitted,
        changed_files=changed_files,
    )
    if json_output:
        print_json(document)
    else:
        _render_human(
            review=review,
            prior_review=prior_review,
            experiment_id=experiment_id,
            experiment_name=experiment_name,
            content_hash=content_hash,
            content_unchanged=content_unchanged,
            submitted=submitted,
            changed_files=changed_files,
        )
    return _result_exit_code(review, final_task)


def iterate(
    path: Annotated[
        Path,
        typer.Argument(help="Path to exactly one local Harbor task directory"),
    ],
    agent: Annotated[
        Optional[str],
        typer.Option("--agent", "-a", help="Explicit pilot agent"),
    ] = None,
    model: Annotated[
        Optional[str],
        typer.Option("--model", "-m", help="Explicit pilot model"),
    ] = None,
    same_sweep: Annotated[
        bool,
        typer.Option(
            "--same-sweep",
            help="Replay every non-baseline config and count from the prior iteration",
        ),
    ] = False,
    environment: Annotated[
        Optional[EnvironmentType],
        typer.Option("--env", help="Execution environment override"),
    ] = None,
    force_build: Annotated[
        bool,
        typer.Option("--force-build", help="Force Harbor environment build behavior"),
    ] = False,
    reuse_pinned_image: Annotated[
        bool,
        typer.Option(
            "--reuse-pinned-image",
            help="Acknowledge that task.toml's docker_image already contains these edits",
        ),
    ] = False,
    registry_login: Annotated[
        Optional[list[str]],
        typer.Option(
            "--registry-login",
            help=(
                "Private registry login as registry=,username=,token= pairs; "
                "repeat for more than one registry"
            ),
        ),
    ] = None,
    api_url: Annotated[
        str,
        typer.Option("--api", help="API URL"),
    ] = "",
    json_output: Annotated[
        bool,
        typer.Option("--json", help="Emit one stable JSON document after completion"),
    ] = False,
) -> None:
    """Run one trusted edit → baselines → model → QA → comparison loop."""

    if (agent is None) != (model is None):
        raise typer.BadParameter("--agent and --model must be supplied together")
    if same_sweep and agent is not None:
        raise typer.BadParameter(
            "--same-sweep is mutually exclusive with --agent/--model"
        )
    if agent is not None and (not agent.strip() or not model or not model.strip()):
        raise typer.BadParameter("--agent and --model must be non-empty")
    if agent is not None and is_nop_oracle_agent(agent):
        raise typer.BadParameter(
            "--agent must select a non-baseline model agent; iterate adds nop and "
            "oracle automatically"
        )

    resolved_api_url = api_url or get_api_url()
    require_api_key(resolved_api_url)
    try:
        registry_auth = parse_registry_login(registry_login, dict(os.environ)) or None
    except ValueError as exc:
        error_console.print(f"[red]Invalid --registry-login:[/red] {exc}")
        raise typer.Exit(1) from exc

    task_paths = resolve_local_task_paths(
        path=path,
        path_option=None,
        dataset=None,
        task_names=None,
        exclude_task_names=None,
        n_tasks=None,
        quiet=json_output,
    )
    if len(task_paths) != 1 or not is_task_dir(path):
        error_console.print(
            "[red]oddish iterate requires a path to exactly one local task "
            f"directory; resolved {len(task_paths)} task(s).[/red]"
        )
        raise typer.Exit(1)

    try:
        exit_code = _run_iteration(
            task_path=task_paths[0],
            api_url=resolved_api_url,
            agent=agent,
            model=model,
            same_sweep=same_sweep,
            environment=environment,
            force_build=force_build,
            reuse_pinned_image=reuse_pinned_image,
            registry_auth=registry_auth,
            json_output=json_output,
        )
    except typer.Exit:
        raise
    except Exception as exc:
        message = str(exc)
        for credential in registry_auth or []:
            token = str(credential.get("token") or "")
            if token:
                message = message.replace(token, "[REDACTED]")
        error_console.print(f"[red]Iteration failed:[/red] {escape(message)}")
        raise typer.Exit(1) from exc

    if exit_code:
        raise typer.Exit(exit_code)
