"""Read existing QA findings through the task-detail API and export CSVs."""

from __future__ import annotations

import csv
import json
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Annotated, Any
from urllib.parse import quote

import httpx
import typer

from oddish.analyze.models import ActionTier
from oddish.cli.config import get_api_url, get_auth_headers

qa_app = typer.Typer(help="Export existing QA feedback.", no_args_is_help=True)

_TASK_COLUMNS = [
    "task_id",
    "task_name",
    "task_status",
    "current_version",
    "current_verdict_status",
    "current_verdict",
]
_ITEM_COLUMNS = [
    "id",
    "source",
    "tier",
    "problem_type",
    "dimension",
    "title",
    "detail",
    "recommendation",
    "file",
    "line_start",
    "line_end",
    "links_to",
    "exploited",
    "exploit_evidence",
    "causal",
]
_FINDING_COLUMNS = (
    _TASK_COLUMNS
    + [
        "task_version_id",
        "task_version",
        "audit_status",
        "trial_id",
        "analysis_status",
        "classification",
        "subtype",
    ]
    + _ITEM_COLUMNS
    + ["group", "assignee", "resolution"]
)
_SUMMARY_COLUMNS = _TASK_COLUMNS + [
    "current_verdict_detail",
    "current_verdict_error",
    "exported_versions",
    "audits",
    "qa_runs",
    "analysis_status_counts",
    "must_fix_count",
    "should_fix_count",
    "optional_count",
    "fetch_error",
]


def _export_rows(
    detail: dict[str, Any], *, tiers: set[str], all_versions: bool
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Flatten source findings without merging separate trial observations."""
    task = detail["task"]
    verdict = task.get("verdict") or {}
    common = {
        "task_id": task["id"],
        "task_name": task["name"],
        "task_status": task["status"],
        "current_version": task.get("current_version"),
        "current_verdict_status": task.get("verdict_status"),
        "current_verdict": verdict.get("verdict"),
    }
    versions = {
        version["id"]: version
        for version in detail["versions"]
        if all_versions or version["id"] == task.get("current_version_id")
    }
    if not versions:
        raise ValueError(
            "Task detail contains no versions matching the requested scope"
        )
    # The detail endpoint already excludes superseded runs and combine copies.
    trials = [
        trial
        for trial in task.get("trials") or []
        if trial.get("task_version_id") in versions
    ]
    rows: list[dict[str, Any]] = []
    for version_id, version in versions.items():
        version_fields = {
            **common,
            "task_version_id": version_id,
            "task_version": version["version"],
            "audit_status": version.get("pre_trial_status"),
        }
        # Audit items belong to the version, so emit them once, not per trial.
        observations = [(item, {}) for item in version.get("pre_trial_findings") or []]
        for trial in trials:
            if (
                trial.get("kind", "agent") != "agent"
                or trial["task_version_id"] != version_id
            ):
                continue
            analysis = trial.get("analysis") or {}
            trial_fields = {
                "trial_id": trial["id"],
                "analysis_status": trial.get("analysis_status"),
                "classification": analysis.get("classification"),
                "subtype": analysis.get("subtype"),
            }
            observations.extend(
                (item, trial_fields) for item in analysis.get("action_items") or []
            )
        for item, trial_fields in observations:
            if item["tier"] in tiers:
                rows.append(
                    {
                        **version_fields,
                        **trial_fields,
                        **{key: item.get(key) for key in _ITEM_COLUMNS},
                    }
                )

    counts = Counter(row["tier"] for row in rows)
    summary = {
        **common,
        "current_verdict_detail": json.dumps(verdict, ensure_ascii=False),
        "current_verdict_error": task.get("verdict_error"),
        "exported_versions": json.dumps([v["version"] for v in versions.values()]),
        "audits": json.dumps(
            [
                {
                    "version": v["version"],
                    "status": v.get("pre_trial_status"),
                    "error": v.get("pre_trial_error"),
                }
                for v in versions.values()
            ],
            ensure_ascii=False,
        ),
        "qa_runs": json.dumps(
            [
                {
                    "id": t["id"],
                    "task_version_id": t["task_version_id"],
                    "status": t["status"],
                    "error": t.get("error_message"),
                }
                for t in trials
                if t.get("kind") == "qa"
            ],
            ensure_ascii=False,
        ),
        "analysis_status_counts": json.dumps(
            Counter(
                t.get("analysis_status") or "not_analyzed"
                for t in trials
                if t.get("kind", "agent") == "agent"
            )
        ),
        **{f"{tier.value}_count": counts[tier.value] for tier in ActionTier},
    }
    return rows, summary


@qa_app.command("export")
def export_qa(
    task_ids: Annotated[
        list[str] | None, typer.Argument(help="Exact task IDs.")
    ] = None,
    ids_file: Annotated[
        Path | None,
        typer.Option(
            "--ids-file",
            exists=True,
            dir_okay=False,
            readable=True,
            help="UTF-8 file with one exact task ID per line.",
        ),
    ] = None,
    output: Annotated[
        Path, typer.Option("--output", "-o", dir_okay=False, help="Findings CSV path.")
    ] = Path("qa-findings.csv"),
    tier: Annotated[
        list[ActionTier] | None,
        typer.Option(
            "--tier",
            help="Include this severity (repeatable; default: must_fix and should_fix).",
        ),
    ] = None,
    all_versions: Annotated[
        bool,
        typer.Option(
            "--all-versions",
            help="Include older task versions; default is current version only.",
        ),
    ] = False,
    concurrency: Annotated[
        int,
        typer.Option(
            "--concurrency",
            min=1,
            max=16,
            help="Maximum simultaneous task-detail requests.",
        ),
    ] = 4,
    api: Annotated[
        str | None, typer.Option("--api", help="Override the API URL.")
    ] = None,
) -> None:
    """Export findings and a companion <output-stem>-tasks.csv task summary.

    Reads existing results only. Duplicate IDs are fetched once. A failed fetch
    is recorded in the summary and causes exit 1 after the other tasks finish.
    """
    refs = list(task_ids or [])
    if ids_file:
        try:
            refs.extend(ids_file.read_text(encoding="utf-8-sig").splitlines())
        except (OSError, UnicodeError) as exc:
            raise typer.BadParameter(str(exc), param_hint="--ids-file") from exc
    refs = list(dict.fromkeys(ref.strip() for ref in refs if ref.strip()))
    if not refs:
        raise typer.BadParameter("Provide task IDs or a nonempty --ids-file")
    summary_path = output.with_name(f"{output.stem}-tasks.csv")
    if ids_file and ids_file.resolve() in {output.resolve(), summary_path.resolve()}:
        raise typer.BadParameter("Output paths must not overwrite --ids-file")
    api_url = (api or get_api_url()).rstrip("/")
    headers = get_auth_headers(api_url)
    tiers = {t.value for t in (tier or [ActionTier.MUST_FIX, ActionTier.SHOULD_FIX])}
    failures = finding_count = 0
    try:
        # One pooled, thread-safe client; the existing endpoint owns QA selection
        # and authentication owns credentials. No per-trial fetch or new API.
        with (
            output.open("w", encoding="utf-8", newline="") as findings_file,
            summary_path.open("w", encoding="utf-8", newline="") as tasks_file,
            httpx.Client(timeout=30.0, headers=headers) as client,
            ThreadPoolExecutor(max_workers=concurrency) as pool,
        ):
            findings_writer = csv.DictWriter(findings_file, fieldnames=_FINDING_COLUMNS)
            tasks_writer = csv.DictWriter(tasks_file, fieldnames=_SUMMARY_COLUMNS)
            findings_writer.writeheader()
            tasks_writer.writeheader()

            def fetch(task_id: str) -> tuple[list[dict[str, Any]], dict[str, Any]]:
                try:
                    response = client.get(
                        f"{api_url}/tasks/{quote(task_id, safe='')}/detail"
                    )
                    response.raise_for_status()
                    return _export_rows(
                        response.json(), tiers=tiers, all_versions=all_versions
                    )
                except (httpx.HTTPError, ValueError, KeyError, TypeError) as exc:
                    return [], {"task_id": task_id, "fetch_error": str(exc)}

            # map preserves input order even when requests finish out of order.
            for index, (rows, summary) in enumerate(pool.map(fetch, refs), start=1):
                findings_writer.writerows(rows)
                tasks_writer.writerow(summary)
                finding_count += len(rows)
                failures += bool(summary.get("fetch_error"))
                typer.echo(
                    f"Exported {index}/{len(refs)} tasks; {finding_count} findings; "
                    f"{failures} fetch errors",
                    err=True,
                )
    except OSError as exc:
        typer.echo(f"Could not write QA export: {exc}", err=True)
        raise typer.Exit(1) from exc
    typer.echo(f"Findings: {output}\nTask summary: {summary_path}")
    if failures:
        raise typer.Exit(1)
