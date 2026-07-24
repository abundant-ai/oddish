from __future__ import annotations

import json as _json
from pathlib import Path
from typing import Annotated, Optional

import httpx
import typer
from rich.console import Console
from rich.table import Table

from oddish.cli.config import get_api_url, get_auth_headers, require_api_key
from oddish.cli.prompt import resolve_scope_flags

console = Console()
err_console = Console(stderr=True)
qa_jobs_app = typer.Typer(
    help="Assign registry prompts as automatic pre-/post-trial QA jobs.",
    no_args_is_help=True,
)

_STAGE_LABEL = {"pre_trial": "PRE", "post_trial": "POST"}


def _resolve(api_url: str | None) -> str:
    url = api_url or get_api_url()
    require_api_key(url)
    return url


def _fail(resp: httpx.Response) -> None:
    console.print(f"[red]Failed ({resp.status_code}):[/red] {resp.text}")
    raise typer.Exit(1)


def _stage(pre_trial: bool, post_trial: bool) -> str:
    if pre_trial and post_trial:
        raise typer.BadParameter("Choose either --pre-trial or --post-trial.")
    if not pre_trial and not post_trial:
        raise typer.BadParameter("Specify --pre-trial or --post-trial.")
    return "pre_trial" if pre_trial else "post_trial"


def _scope_params(scope: str, scope_id: Optional[str]) -> dict:
    params: dict = {"scope": scope}
    if scope_id:
        params["scope_id"] = scope_id
    return params


def _scope_display(scope: str, scope_id: Optional[str]) -> str:
    return f"{scope}{':' + scope_id if scope_id else ''}"


_ScopeOrg = Annotated[bool, typer.Option("--org", help="The current organization.")]
_ScopeUser = Annotated[bool, typer.Option("--user", help="The authenticated user.")]
_ScopeTask = Annotated[Optional[str], typer.Option("--task", help="A task id.")]
_ScopeExperiment = Annotated[
    Optional[str], typer.Option("--experiment", help="An experiment id.")
]
_ScopeTrial = Annotated[Optional[str], typer.Option("--trial", help="A trial id.")]
_ScopeGlobal = Annotated[
    bool, typer.Option("--global", help="The installation-wide default.")
]
_PreTrial = Annotated[bool, typer.Option("--pre-trial", help="Run before trials.")]
_PostTrial = Annotated[bool, typer.Option("--post-trial", help="Run after each trial.")]


@qa_jobs_app.command("assign")
def assign(
    prompt: Annotated[str, typer.Argument(help="Prompt kind, or prompt id.")],
    pre_trial: _PreTrial = False,
    post_trial: _PostTrial = False,
    file: Annotated[
        Optional[Path],
        typer.Option(
            "--file",
            "-f",
            help="Upload this content as a new prompt version at the same scope first.",
        ),
    ] = None,
    model: Annotated[Optional[str], typer.Option("--model", "-m")] = None,
    backend: Annotated[
        Optional[str], typer.Option("--backend", help="sandbox or api")
    ] = None,
    reasoning_effort: Annotated[
        Optional[str], typer.Option("--reasoning-effort")
    ] = None,
    prompt_version: Annotated[
        Optional[int],
        typer.Option("--prompt-version", help="Pin a version; default is latest-wins."),
    ] = None,
    allow_oddish_cli: Annotated[
        Optional[bool],
        typer.Option(
            "--allow-oddish-cli/--no-allow-oddish-cli",
            help="Grant the sandbox agent the oddish CLI. Omit to leave an "
            "existing job's setting unchanged.",
        ),
    ] = None,
    org: _ScopeOrg = False,
    user: _ScopeUser = False,
    task: _ScopeTask = None,
    experiment: _ScopeExperiment = None,
    trial: _ScopeTrial = None,
    global_scope: _ScopeGlobal = False,
    api_url: Annotated[Optional[str], typer.Option("--api-url", "-u")] = None,
):
    """Assign a prompt as a QA job; organization scope is the default."""
    stage = _stage(pre_trial, post_trial)
    if backend not in {None, "sandbox", "api"}:
        raise typer.BadParameter("--backend must be sandbox or api")
    if reasoning_effort not in {None, "low", "medium", "high"}:
        raise typer.BadParameter("--reasoning-effort must be low, medium, or high")
    url = _resolve(api_url)
    scope, scope_id = resolve_scope_flags(
        org=org,
        user=user,
        task=task,
        experiment=experiment,
        trial=trial,
        global_scope=global_scope,
    )
    params = _scope_params(scope, scope_id)

    with httpx.Client(timeout=30.0, headers=get_auth_headers()) as client:
        if file is not None:
            uploaded = client.put(
                f"{url}/prompts/{prompt}",
                params=params,
                json={"content": file.read_text()},
            )
            if uploaded.status_code != 200:
                _fail(uploaded)
            body = uploaded.json()
            console.print(
                f"[green]✓[/green] {body['kind']} v{body.get('latest_version')} "
                f"({_scope_display(scope, scope_id)})"
            )
        payload = {
            "prompt": prompt,
            "stage": stage,
            "prompt_version": prompt_version,
            "model": model,
            "backend": backend,
            "reasoning_effort": reasoning_effort,
            "allow_oddish_cli": allow_oddish_cli,
            "enabled": True,
        }
        resp = client.post(f"{url}/qa-jobs", params=params, json=payload)
    if resp.status_code != 200:
        _fail(resp)
    job = resp.json()
    console.print(
        f"[green]✓[/green] assigned {_STAGE_LABEL[job['stage']]} · {job['backend']} · "
        f"{job['model']} · {job['prompt_kind']} v{job.get('effective_version')} "
        f"({_scope_display(scope, scope_id)})"
    )


@qa_jobs_app.command("list")
def list_jobs(
    defined: Annotated[
        bool,
        typer.Option(
            "--defined",
            help="Only rows set at exactly this scope, not the inherited set.",
        ),
    ] = False,
    stage: Annotated[
        Optional[str], typer.Option("--stage", help="pre_trial or post_trial")
    ] = None,
    org: _ScopeOrg = False,
    user: _ScopeUser = False,
    task: _ScopeTask = None,
    experiment: _ScopeExperiment = None,
    trial: _ScopeTrial = None,
    global_scope: _ScopeGlobal = False,
    json_output: Annotated[bool, typer.Option("--json")] = False,
    api_url: Annotated[Optional[str], typer.Option("--api-url", "-u")] = None,
):
    """Show which QA jobs a scope will actually run."""
    if stage not in {None, "pre_trial", "post_trial"}:
        raise typer.BadParameter("--stage must be pre_trial or post_trial")
    url = _resolve(api_url)
    scope, scope_id = resolve_scope_flags(
        org=org,
        user=user,
        task=task,
        experiment=experiment,
        trial=trial,
        global_scope=global_scope,
        default="global",
    )
    params = _scope_params(scope, scope_id)
    params["resolved"] = "false" if defined else "true"
    if stage:
        params["stage"] = stage
    with httpx.Client(timeout=30.0, headers=get_auth_headers()) as client:
        resp = client.get(f"{url}/qa-jobs", params=params)
    if resp.status_code != 200:
        _fail(resp)
    rows = resp.json()
    if json_output:
        console.print_json(_json.dumps(rows))
        return
    # Always report the scope that was read: reads default to global while
    # writes default to org, so an unlabelled listing silently misleads.
    err_console.print(
        f"[dim]scope={_scope_display(scope, scope_id)} "
        f"({'defined here' if defined else 'resolved'})[/dim]"
    )
    if not rows:
        console.print("[dim]no QA jobs[/dim]")
        return
    table = Table("PHASE", "PROMPT", "VER", "RUNNER", "MODEL", "FROM")
    for row in rows:
        version = row.get("effective_version")
        table.add_row(
            _STAGE_LABEL.get(row["stage"], row["stage"]),
            row["prompt_kind"] + ("" if row["enabled"] else " (disabled)"),
            f"v{version}" if version else "—",
            row["backend"],
            row["model"],
            row["inherited_from"],
        )
    console.print(table)


@qa_jobs_app.command("status")
def status(
    org: _ScopeOrg = False,
    user: _ScopeUser = False,
    task: _ScopeTask = None,
    experiment: _ScopeExperiment = None,
    trial: _ScopeTrial = None,
    global_scope: _ScopeGlobal = False,
    json_output: Annotated[bool, typer.Option("--json")] = False,
    api_url: Annotated[Optional[str], typer.Option("--api-url", "-u")] = None,
):
    """Show execution counts for the effective assignments at a scope."""
    url = _resolve(api_url)
    scope, scope_id = resolve_scope_flags(
        org=org,
        user=user,
        task=task,
        experiment=experiment,
        trial=trial,
        global_scope=global_scope,
        default="global",
    )
    with httpx.Client(timeout=30.0, headers=get_auth_headers()) as client:
        resp = client.get(
            f"{url}/qa-jobs/status",
            params=_scope_params(scope, scope_id),
        )
    if resp.status_code != 200:
        _fail(resp)
    body = resp.json()
    if json_output:
        console.print_json(_json.dumps(body))
        return
    rows = body.get("jobs") or []
    err_console.print(f"[dim]scope={body.get('scope') or scope}[/dim]")
    if not rows:
        console.print("[dim]no QA job runs[/dim]")
        return
    table = Table("PHASE", "PROMPT", "FROM", "QUEUED", "RUNNING", "SUCCESS", "FAILED")
    for row in rows:
        counts = row.get("counts") or {}
        table.add_row(
            _STAGE_LABEL.get(row["stage"], row["stage"]),
            row["prompt_kind"],
            row["inherited_from"],
            str(counts.get("QUEUED", 0)),
            str(counts.get("RUNNING", 0)),
            str(counts.get("SUCCESS", 0)),
            str(counts.get("FAILED", 0)),
        )
    console.print(table)


@qa_jobs_app.command("disable")
def disable(
    prompt: Annotated[str, typer.Argument(help="Prompt kind, or prompt id.")],
    pre_trial: _PreTrial = False,
    post_trial: _PostTrial = False,
    org: _ScopeOrg = False,
    user: _ScopeUser = False,
    task: _ScopeTask = None,
    experiment: _ScopeExperiment = None,
    trial: _ScopeTrial = None,
    global_scope: _ScopeGlobal = False,
    api_url: Annotated[Optional[str], typer.Option("--api-url", "-u")] = None,
):
    """Suppress a broader default at this scope by writing a disabled row."""
    stage = _stage(pre_trial, post_trial)
    url = _resolve(api_url)
    scope, scope_id = resolve_scope_flags(
        org=org,
        user=user,
        task=task,
        experiment=experiment,
        trial=trial,
        global_scope=global_scope,
    )
    with httpx.Client(timeout=30.0, headers=get_auth_headers()) as client:
        resp = client.post(
            f"{url}/qa-jobs",
            params=_scope_params(scope, scope_id),
            json={"prompt": prompt, "stage": stage, "enabled": False},
        )
    if resp.status_code != 200:
        _fail(resp)
    job = resp.json()
    console.print(
        f"[green]✓[/green] disabled {_STAGE_LABEL[job['stage']]} · "
        f"{job['prompt_kind']} ({_scope_display(scope, scope_id)})"
    )
