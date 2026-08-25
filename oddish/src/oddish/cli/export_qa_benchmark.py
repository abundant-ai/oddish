"""Export human-reviewed solver and judge trials for offline QA evaluation."""

from __future__ import annotations

import json
import tarfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from typing import Annotated, Optional

import httpx
import typer
from rich.console import Console

from oddish.cli.config import get_api_url, print_json, require_api_key
from oddish.cli.pull import _make_client, _pull_trial, _write_json


console = Console()
error_console = Console(stderr=True)

_BUNDLE_README = """# Oddish human-reviewed QA benchmark

`samples.jsonl` contains one reviewed solver trial per line. `human_vote` is
the human label: `agree` means the QA classification in `classification` was
judged correct; `disagree` means it was judged incorrect. `review_note` is the
optional explanation submitted with that vote. Reviewer identities are not
included.

`solver_trial_path` points to the attempted task run. `grader_trial_path`
points to the task-wide QA agent run that produced the solver trial's current
classification and summary. Several samples can reference the same grader.

Each trial directory contains its Oddish detail response, permanent plain and
structured logs, Harbor result, ATIF trajectory, and stored trajectory summary.
The selection excludes conflicting votes and labels invalidated by a later QA
run. See `manifest.json` for counts and download failures.
"""


def _default_output(limit: int) -> Path:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return Path.cwd() / f"oddish-qa-benchmark-{limit}-{stamp}"


def _write_jsonl(path: Path, items: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        for item in items:
            handle.write(json.dumps(item, default=str, separators=(",", ":")))
            handle.write("\n")


def _download_one(client: httpx.Client, trial_id: str, output_root: Path) -> dict:
    row = _pull_trial(
        client,
        trial_id,
        output_root,
        include_logs=True,
        include_files=False,
        include_structured_logs=True,
        status_update=None,
    )
    trial_root = output_root / "trials" / trial_id
    required = (
        "trial.json",
        "logs.txt",
        "logs_structured.json",
        "result.json",
        "trajectory.json",
        "trajectory_summary.json",
    )
    missing = [name for name in required if not (trial_root / name).is_file()]
    if missing:
        row["errors"] = int(row.get("errors") or 0) + len(missing)
        row["missing_files"] = missing
    return row


def export_qa_benchmark(
    limit: Annotated[
        int,
        typer.Option(
            "--limit", min=1, max=1000, help="Reviewed solver trials to export."
        ),
    ] = 300,
    out: Annotated[
        Optional[Path],
        typer.Option("--out", "-o", help="New directory for the export bundle."),
    ] = None,
    workers: Annotated[
        int,
        typer.Option("--workers", min=1, max=32, help="Concurrent trial downloads."),
    ] = 8,
    allow_fewer: Annotated[
        bool,
        typer.Option(
            "--allow-fewer",
            help="Export every eligible label instead of failing when fewer than --limit exist.",
        ),
    ] = False,
    archive: Annotated[
        bool,
        typer.Option(
            "--archive/--no-archive", help="Create a .tar.gz beside the directory."
        ),
    ] = True,
    api_url: Annotated[str, typer.Option("--api", help="API URL")] = "",
    json_output: Annotated[
        bool,
        typer.Option("--json", help="Print the final export manifest as JSON."),
    ] = False,
) -> None:
    """Export labeled solver trials and the QA trials that graded them.

    Hosted Oddish only. The label-selection endpoint requires a full-scope API
    key in the configured operator organization; every downloaded resource is
    then read through the ordinary org-scoped trial endpoints.
    """

    if not api_url:
        api_url = get_api_url()
    require_api_key(api_url)

    try:
        with _make_client(api_url) as client:
            response = client.get("/admin/qa-feedback-export", params={"limit": limit})
            if response.status_code != 200:
                if json_output:
                    print_json({"error": response.text, "status": response.status_code})
                elif response.status_code in (401, 403):
                    error_console.print(
                        "[red]QA benchmark export requires a full-scope API key "
                        "in the operator organization.[/red]"
                    )
                else:
                    error_console.print(
                        f"[red]Failed to select QA feedback:[/red] {response.text}"
                    )
                raise typer.Exit(1)

            selection = response.json()
            items = selection.get("items") or []
            if len(items) < limit and not allow_fewer:
                detail = (
                    f"Production has {selection.get('eligible_total', len(items))} eligible "
                    f"human-reviewed trials; {limit} were requested. Re-run with "
                    "--allow-fewer to export the available set."
                )
                if json_output:
                    print_json({"error": detail, "selection": selection})
                else:
                    error_console.print(f"[red]{detail}[/red]")
                raise typer.Exit(1)

            output_root = out or _default_output(limit)
            archive_path = Path(f"{output_root}.tar.gz") if archive else None
            if output_root.exists():
                if not output_root.is_dir():
                    raise typer.BadParameter(f"--out is not a directory: {output_root}")
                if any(output_root.iterdir()):
                    raise typer.BadParameter(
                        f"--out must be new or empty; found files in {output_root}"
                    )
            if archive_path is not None and archive_path.exists():
                raise typer.BadParameter(
                    f"archive already exists; choose another --out: {archive_path}"
                )
            output_root.mkdir(parents=True, exist_ok=True)
            (output_root / "README.md").write_text(_BUNDLE_README, encoding="utf-8")

            samples: list[dict] = []
            trial_ids: set[str] = set()
            for item in items:
                solver_id = item["trial_id"]
                grader_id = item["grader_trial_id"]
                trial_ids.update((solver_id, grader_id))
                samples.append(
                    {
                        **item,
                        "solver_trial_path": f"trials/{solver_id}",
                        "grader_trial_path": f"trials/{grader_id}",
                    }
                )

            _write_jsonl(output_root / "samples.jsonl", samples)
            _write_json(output_root / "selection.json", selection)

            download_rows: list[dict] = []
            ordered_ids = sorted(trial_ids)
            distinct_grader_ids = {item["grader_trial_id"] for item in items}
            if not json_output:
                console.print(
                    f"[cyan]Downloading[/cyan] {len(items)} labeled solver trials and "
                    f"{len(distinct_grader_ids)} distinct QA judge trials -> "
                    f"{output_root}"
                )
            with ThreadPoolExecutor(max_workers=workers) as pool:
                futures = {
                    pool.submit(_download_one, client, trial_id, output_root): trial_id
                    for trial_id in ordered_ids
                }
                for future in as_completed(futures):
                    trial_id = futures[future]
                    try:
                        download_rows.append(future.result())
                    except Exception as exc:  # noqa: BLE001 -- preserve partial bundle
                        download_rows.append(
                            {"trial_id": trial_id, "errors": 1, "detail": str(exc)}
                        )

    except httpx.HTTPError as exc:
        error_console.print(f"[red]Failed to connect to API:[/red] {exc}")
        raise typer.Exit(1) from exc

    failed = [row for row in download_rows if int(row.get("errors") or 0) > 0]
    manifest = {
        "format": "oddish-qa-benchmark:v1",
        "api_url": api_url,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "requested_labels": limit,
        "exported_labels": len(items),
        "eligible_total": selection.get("eligible_total", len(items)),
        "unique_trials_downloaded": len(download_rows),
        "failed_trials": failed,
        "output_directory": str(output_root.resolve()),
        "archive": None,
    }
    if failed:
        _write_json(output_root / "manifest.json", manifest)
        if json_output:
            print_json(manifest)
        else:
            error_console.print(
                f"[red]Export is incomplete: {len(failed)} trial downloads failed. "
                f"See {output_root / 'manifest.json'}.[/red]"
            )
        raise typer.Exit(1)

    if archive:
        assert archive_path is not None
        manifest["archive"] = str(archive_path.resolve())
        _write_json(output_root / "manifest.json", manifest)
        with tarfile.open(archive_path, "w:gz") as tar:
            tar.add(output_root, arcname=output_root.name)
    else:
        _write_json(output_root / "manifest.json", manifest)

    if json_output:
        print_json(manifest)
    else:
        console.print(
            f"[green]Export complete[/green]: {len(items)} labels, "
            f"{len(download_rows)} unique solver/judge trials"
        )
        console.print(f"[dim]Directory: {output_root.resolve()}[/dim]")
        if manifest["archive"]:
            console.print(f"[dim]Archive: {manifest['archive']}[/dim]")


__all__ = ["export_qa_benchmark"]
