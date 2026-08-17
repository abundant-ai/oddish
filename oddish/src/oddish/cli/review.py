"""Read and render existing task-version QA without starting analysis."""

from __future__ import annotations

import time
from typing import Annotated, Callable, Optional

import httpx
import typer
from pydantic import ValidationError
from rich.console import Console
from rich.markup import escape
from rich.table import Table
from rich.text import Text

from oddish.analyze.models import ActionTier
from oddish.cli.api import get_task_review
from oddish.cli.config import get_api_url, print_json, require_api_key
from oddish.schemas import TaskReviewFinding, TaskReviewResponse, TaskReviewTrial

console = Console()
error_console = Console(stderr=True)

_TIER_LABELS = {
    ActionTier.MUST_FIX: "MUST FIX",
    ActionTier.SHOULD_FIX: "SHOULD FIX",
    ActionTier.OPTIONAL: "OPTIONAL",
}


def _same_review(first: TaskReviewResponse, page: TaskReviewResponse) -> None:
    if (
        page.schema_version != first.schema_version
        or page.task != first.task
        or page.scope != first.scope
        or page.finding_counts != first.finding_counts
        or page.trial_counts != first.trial_counts
        or page.baselines != first.baselines
        or page.qa != first.qa
        or page.verdict != first.verdict
    ):
        raise ValueError("Task review changed while its pages were being read")


_WAIT_POLL_SECONDS = 5.0
_WAIT_DEFAULT_TIMEOUT_SECONDS = 900.0
_ACTIVE_QA_STATUSES = {"pending", "queued", "running"}


def _qa_active(response: TaskReviewResponse) -> bool:
    if response.qa.active_run is not None:
        return True
    status = response.qa.status
    return status is not None and status.value in _ACTIVE_QA_STATUSES


def wait_for_qa(
    api_url: str,
    task_ref: str,
    *,
    version: int | None,
    experiment_id: str | None,
    tiers: list[ActionTier] | None,
    timeout_seconds: float,
    sleep: Callable[[float], None] | None = None,
) -> bool:
    """Poll lightweight review pages until no QA run is active.

    Read-only: this never enqueues analysis. Returns True when QA settled,
    False when *timeout_seconds* elapsed with QA still active.
    """

    sleep = sleep or time.sleep
    deadline = time.monotonic() + timeout_seconds
    while True:
        page = get_task_review(
            api_url,
            task_ref,
            version=version,
            experiment_id=experiment_id,
            tiers=tiers,
            finding_limit=0,
            trial_limit=0,
        )
        if not _qa_active(page):
            return True
        if time.monotonic() >= deadline:
            return False
        sleep(min(_WAIT_POLL_SECONDS, max(deadline - time.monotonic(), 0.0)))


def fetch_complete_review(
    api_url: str,
    task_ref: str,
    *,
    version: int | None,
    experiment_id: str | None,
    tiers: list[ActionTier] | None,
) -> TaskReviewResponse:
    """Follow each independent cursor once and return one complete document."""

    first = get_task_review(
        api_url,
        task_ref,
        version=version,
        experiment_id=experiment_id,
        tiers=tiers,
    )
    findings = list(first.findings)
    trials = list(first.trials)

    page = first
    seen_finding_cursors: set[str] = set()
    while page.findings_page.has_more:
        cursor = page.findings_page.next_cursor
        if cursor is None or cursor in seen_finding_cursors:
            raise ValueError("Task review returned an invalid finding cursor chain")
        seen_finding_cursors.add(cursor)
        page = get_task_review(
            api_url,
            task_ref,
            version=version,
            experiment_id=experiment_id,
            tiers=tiers,
            finding_cursor=cursor,
            trial_limit=0,
        )
        _same_review(first, page)
        findings.extend(page.findings)

    page = first
    seen_trial_cursors: set[str] = set()
    while page.trials_page.has_more:
        cursor = page.trials_page.next_cursor
        if cursor is None or cursor in seen_trial_cursors:
            raise ValueError("Task review returned an invalid trial cursor chain")
        seen_trial_cursors.add(cursor)
        page = get_task_review(
            api_url,
            task_ref,
            version=version,
            experiment_id=experiment_id,
            tiers=tiers,
            finding_limit=0,
            trial_cursor=cursor,
        )
        _same_review(first, page)
        trials.extend(page.trials)

    expected_trials = (
        first.trial_counts.eligible
        + first.baselines.nop.trial_count
        + first.baselines.oracle.trial_count
    )
    if len(findings) != first.finding_counts.filtered_total:
        raise ValueError("Task review finding pages did not match their exact total")
    if len(trials) != expected_trials:
        raise ValueError("Task review trial pages did not match their exact total")

    combined = first.model_copy(deep=True)
    combined.findings = findings
    combined.trials = trials
    combined.findings_page.has_more = False
    combined.findings_page.next_cursor = None
    combined.trials_page.has_more = False
    combined.trials_page.next_cursor = None
    return combined


def _number(value: float | None) -> str:
    if value is None:
        return "-"
    if value == int(value):
        return str(int(value))
    return f"{value:.2f}"


def _baseline_line(response: TaskReviewResponse, role: str) -> str:
    summary = getattr(response.baselines, role)
    row = next((trial for trial in response.trials if trial.role == role), None)
    reward = row.reward if row is not None else summary.expected_reward
    mark = "✓" if reward == 1 else "✗" if reward == 0 else "?"
    expectation = "expected" if summary.valid else "unexpected"
    count = f" · {summary.trial_count} trials" if summary.trial_count != 1 else ""
    return f"{mark} reward {_number(reward)} · {expectation}{count}"


def _render_finding(finding: TaskReviewFinding) -> None:
    console.print(
        f"[cyan]{escape(finding.id)}[/cyan]  "
        f"{escape(finding.dimension.value)} / {escape(finding.problem_type.value)}"
    )
    line = (
        str(finding.line_start)
        if finding.line_start == finding.line_end
        else f"{finding.line_start}-{finding.line_end}"
    )
    console.print(f"{escape(finding.file)}:{line}")
    console.print(Text(finding.title, style="bold"))
    console.print("\n  [bold]QA said[/bold]")
    console.print(Text(f"  {finding.detail}"))
    console.print("\n  [bold]Suggested fix[/bold]")
    console.print(Text(f"  {finding.recommendation}"))
    provenance = [finding.source.value]
    if finding.trial_ids:
        provenance.append(f"seen in {', '.join(finding.trial_ids)}")
    if finding.exploited:
        provenance.append(f"exploited: {finding.exploit_evidence or 'yes'}")
    console.print("\n  [bold]Provenance[/bold]")
    console.print(Text(f"  {' · '.join(provenance)}"))


def _trial_qa(trial: TaskReviewTrial) -> tuple[str, str]:
    reward = trial.reward
    verifier = (
        f"Verifier ✓ reward {_number(reward)}"
        if reward == 1
        else f"Verifier ✗ reward {_number(reward)}"
        if reward is not None
        else "Verifier ? reward -"
    )
    classification = (
        trial.analysis.classification.replace("_", " ").lower()
        if trial.analysis is not None
        else trial.analysis_status.value
        if trial.analysis_status is not None
        else "not analyzed"
    )
    return verifier, f"QA {classification}"


def render_review(
    response: TaskReviewResponse,
    *,
    title: str = "TASK REVIEW",
    read_only_notice: bool = True,
) -> None:
    """Render stored review fields; never paraphrase or recompute QA."""

    console.print(
        f"[bold]{escape(title)}[/bold]  "
        f"{escape(response.task.name)} · v{response.task.version}"
    )
    console.print(f"[bold]Task[/bold]          {escape(response.task.id)}")
    run = response.qa.result_run or response.qa.active_run
    if run is not None:
        state = run.disposition.value if run.disposition is not None else "active"
        console.print(
            f"[bold]QA run[/bold]        {escape(run.id)} · {state} · "
            f"{run.input_trial_count} input trials"
        )
    else:
        console.print("[bold]QA run[/bold]        -")
    experiment = response.scope.experiment_id or "all experiments"
    tiers = ", ".join(tier.value for tier in response.scope.tiers)
    console.print(f"[bold]Scope[/bold]         {escape(experiment)} · {escape(tiers)}")

    console.print("\n[bold]BASELINES[/bold]")
    console.print(f"[bold]nop[/bold]           {_baseline_line(response, 'nop')}")
    console.print(f"[bold]oracle[/bold]        {_baseline_line(response, 'oracle')}")
    console.print(f"[bold]gate[/bold]          {response.baselines.outcome.upper()}")

    console.print("\n[bold]VERDICT[/bold]")
    if response.verdict is None:
        console.print("No version-scoped verdict is available.")
    else:
        console.print(
            f"{response.verdict.verdict.upper()} · "
            f"{response.verdict.confidence} confidence"
        )
        if response.verdict.primary_issue:
            console.print("[bold]Primary issue[/bold]")
            console.print(Text(response.verdict.primary_issue))

    for tier in response.scope.tiers:
        tier_findings = [
            finding for finding in response.findings if finding.tier == tier
        ]
        if not tier_findings:
            continue
        console.print(f"\n[bold]{_TIER_LABELS[tier]} ({len(tier_findings)})[/bold]")
        for index, finding in enumerate(tier_findings):
            if index:
                console.print()
            _render_finding(finding)

    model_trials = [trial for trial in response.trials if trial.role == "model"]
    if model_trials:
        table = Table(title="TRIAL QA", show_header=True)
        table.add_column("Trial")
        table.add_column("Agent")
        table.add_column("Model")
        table.add_column("Verifier")
        table.add_column("QA")
        for trial in model_trials:
            verifier, qa = _trial_qa(trial)
            table.add_row(
                trial.id,
                trial.agent,
                trial.model or "-",
                verifier,
                qa,
            )
        console.print()
        console.print(table)

    console.print(
        f"\nShowing {response.finding_counts.filtered_total}/"
        f"{response.finding_counts.unfiltered_total} findings after filter."
    )
    if read_only_notice:
        console.print("[dim]No analysis was run by this command.[/dim]")


def _warn_review_state(response: TaskReviewResponse) -> None:
    if response.qa.legacy_unscoped_verdict_available:
        error_console.print(
            "[yellow]Warning: the stored task verdict predates version-owned QA "
            "provenance and is not shown.[/yellow]"
        )
        error_console.print(f"Run: oddish run {response.task.id} --retry --qa")
    if response.qa.input_analysis_changed_after_run:
        error_console.print(
            "[yellow]Warning: one or more trial analyses changed after the "
            "published QA run.[/yellow]"
        )


def _http_error(exc: httpx.HTTPStatusError) -> str:
    try:
        payload = exc.response.json()
    except ValueError:
        return exc.response.text or str(exc)
    if isinstance(payload, dict) and payload.get("detail"):
        return str(payload["detail"])
    return exc.response.text or str(exc)


def review(
    task_ref: Annotated[
        str,
        typer.Argument(help="Exact task ID or exact organization-unique task name"),
    ],
    version: Annotated[
        Optional[int],
        typer.Option("--version", min=1, help="Review this task version"),
    ] = None,
    experiment_id: Annotated[
        Optional[str],
        typer.Option(
            "--experiment",
            help="Narrow visible trial, baseline, and finding evidence",
        ),
    ] = None,
    tier: Annotated[
        Optional[list[ActionTier]],
        typer.Option(
            "--tier",
            help="Finding tier to include; repeat for more than one",
        ),
    ] = None,
    fail_on_findings: Annotated[
        bool,
        typer.Option(
            "--fail-on-findings",
            help="Exit 2 when the selected tier scope contains findings",
        ),
    ] = False,
    wait: Annotated[
        bool,
        typer.Option(
            "--wait",
            help="Poll until the active QA run settles before rendering "
            "(read-only; never starts analysis)",
        ),
    ] = False,
    wait_timeout: Annotated[
        float,
        typer.Option(
            "--wait-timeout",
            min=1.0,
            help="Seconds --wait polls before giving up (default 900)",
        ),
    ] = _WAIT_DEFAULT_TIMEOUT_SECONDS,
    api_url: Annotated[
        str,
        typer.Option("--api", help="API URL"),
    ] = "",
    json_output: Annotated[
        bool,
        typer.Option("--json", help="Emit the complete stable review document"),
    ] = False,
) -> None:
    """Read existing version-pinned task QA without starting analysis."""

    api_url = api_url or get_api_url()
    try:
        require_api_key(api_url)
        if wait and not wait_for_qa(
            api_url,
            task_ref,
            version=version,
            experiment_id=experiment_id,
            tiers=tier,
            timeout_seconds=wait_timeout,
        ):
            error_console.print(
                f"[yellow]Warning: QA is still active after {wait_timeout:.0f}s; "
                "showing its current state.[/yellow]"
            )
        response = fetch_complete_review(
            api_url,
            task_ref,
            version=version,
            experiment_id=experiment_id,
            tiers=tier,
        )
    except typer.Exit:
        raise
    except httpx.HTTPStatusError as exc:
        error_console.print(f"[red]Review request failed:[/red] {_http_error(exc)}")
        raise typer.Exit(1) from None
    except httpx.HTTPError as exc:
        error_console.print(f"[red]Review request failed:[/red] {exc}")
        raise typer.Exit(1) from None
    except (ValidationError, ValueError) as exc:
        error_console.print(f"[red]Malformed review response:[/red] {exc}")
        raise typer.Exit(1) from None

    _warn_review_state(response)
    if json_output:
        print_json(response.model_dump(mode="json"))
    else:
        render_review(response)

    if fail_on_findings and response.finding_counts.filtered_total:
        raise typer.Exit(2)


__all__ = ["fetch_complete_review", "render_review", "review", "wait_for_qa"]
