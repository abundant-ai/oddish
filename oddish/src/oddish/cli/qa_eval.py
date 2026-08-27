from __future__ import annotations

import csv
import json
from pathlib import Path
from typing import Annotated, Optional

import httpx
import typer
from rich.console import Console

from oddish.cli.config import get_api_url, get_auth_headers, print_json, require_api_key
from oddish.core.idempotency import compute_request_hash
from oddish.schemas import QAEvalCreateRequest

console = Console()
qa_eval_app = typer.Typer(
    help="Replay candidate QA prompts over exact historical solver trials.",
    no_args_is_help=True,
)

_NEW_RESULT_COLUMNS = [
    "new_qa_trial_id",
    "new_qa_status",
    "new_qa_analysis_status",
    "new_qa_classification",
    "new_qa_subtype",
    "new_qa_evidence",
    "new_qa_root_cause",
    "new_qa_recommendation",
    "new_qa_action_items_json",
    "new_qa_exploitation_json",
    "new_qa_output_json",
    "new_qa_error",
    "prompt_name",
    "prompt_sha256",
    "qa_model",
]


def _read_case_rows(path: Path) -> tuple[list[str], list[dict[str, str]]]:
    with path.open(newline="", encoding="utf-8-sig") as handle:
        reader = csv.DictReader(handle)
        if not reader.fieldnames or "source_trial_id" not in reader.fieldnames:
            raise ValueError("cases CSV must contain a source_trial_id column")
        rows = [{key: str(value or "") for key, value in row.items()} for row in reader]
    if not any(row["source_trial_id"].strip() for row in rows):
        raise ValueError("cases CSV contains no source_trial_id values")
    return list(reader.fieldnames), rows


def _read_cases(path: Path) -> list[str]:
    _, rows = _read_case_rows(path)
    return list(
        dict.fromkeys(
            source_id for row in rows if (source_id := row["source_trial_id"].strip())
        )
    )


def _read_prompt_spec(spec: str) -> tuple[str, str]:
    prompt_name, separator, raw_path = spec.partition("=")
    prompt_name = prompt_name.strip()
    path = Path(raw_path.strip()).expanduser()
    if not separator or not prompt_name or not raw_path.strip():
        raise ValueError("--prompt must use NAME=PATH")
    if not path.is_file():
        raise ValueError(f"prompt file does not exist: {path}")
    text = path.read_text(encoding="utf-8").strip()
    if not text:
        raise ValueError(f"prompt file is empty: {path}")
    return prompt_name, text


def _experiment_name(base_name: str | None, prompt_name: str, count: int) -> str:
    base = (base_name or "").strip()
    if not base:
        return f"qa-eval-{prompt_name}"
    return base if count == 1 else f"{base}-{prompt_name}"


@qa_eval_app.command("run")
def run_qa_eval(
    cases: Annotated[
        Path,
        typer.Option(
            "--cases",
            exists=True,
            dir_okay=False,
            readable=True,
            help="CSV containing a source_trial_id column.",
        ),
    ],
    prompts: Annotated[
        Optional[list[str]],
        typer.Option(
            "--prompt",
            help="Candidate prompt as NAME=PATH; repeat for several prompts.",
        ),
    ] = None,
    name: Annotated[
        Optional[str],
        typer.Option("--name", help="Experiment name or shared prefix."),
    ] = None,
    model: Annotated[
        Optional[str],
        typer.Option("--model", help="QA model; omitted uses production QA."),
    ] = None,
    api_url: Annotated[str, typer.Option("--api", help="API URL")] = "",
    json_output: Annotated[
        bool, typer.Option("--json", help="Output machine-readable JSON.")
    ] = False,
) -> None:
    """Queue one pointer-based replay experiment per prompt."""
    try:
        source_trial_ids = _read_cases(cases)
        parsed_prompts = [_read_prompt_spec(spec) for spec in (prompts or [])]
    except (OSError, UnicodeError, ValueError) as exc:
        console.print(f"[red]Invalid QA-eval input:[/red] {exc}")
        raise typer.Exit(1) from exc
    if not parsed_prompts:
        console.print("[red]Provide at least one --prompt NAME=PATH.[/red]")
        raise typer.Exit(1)

    api_url = (api_url or get_api_url()).rstrip("/")
    require_api_key(api_url)
    queued = []
    failures = []
    with httpx.Client(timeout=120.0, headers=get_auth_headers(api_url)) as client:
        for prompt_name, prompt_text in parsed_prompts:
            payload = QAEvalCreateRequest(
                name=_experiment_name(name, prompt_name, len(parsed_prompts)),
                source_trial_ids=source_trial_ids,
                prompt_name=prompt_name,
                prompt_text=prompt_text,
                model=(model or "").strip() or None,
            )
            try:
                response = client.post(
                    f"{api_url}/qa-evals",
                    json=payload.model_dump(mode="json"),
                    headers={"Idempotency-Key": compute_request_hash(payload)},
                )
            except httpx.RequestError as exc:
                failures.append({"prompt_name": prompt_name, "error": str(exc)})
                continue
            if response.status_code != 200:
                failures.append(
                    {
                        "prompt_name": prompt_name,
                        "status": response.status_code,
                        "error": response.text,
                    }
                )
                continue
            queued.append(response.json())

    if json_output:
        print_json({"queued": queued, "failed": failures})
    else:
        for result in queued:
            console.print(
                f"[green]Queued[/green] {len(result['trials'])} replay(s) in "
                f"{result['experiment_name']} ({result['experiment_id']})"
            )
        for failure in failures:
            console.print(
                f"[red]Failed[/red] {failure['prompt_name']}: "
                f"{failure.get('status', 'connection')} - {failure['error']}"
            )
    if failures:
        raise typer.Exit(1)


@qa_eval_app.command("collect")
def collect_qa_eval(
    experiment_id: Annotated[str, typer.Argument(help="Exact QA-eval experiment ID.")],
    cases: Annotated[
        Path,
        typer.Option(
            "--cases",
            exists=True,
            dir_okay=False,
            readable=True,
            help="Original CSV used to create the replay.",
        ),
    ],
    out: Annotated[Path, typer.Option("--out", help="Output CSV path.")],
    api_url: Annotated[str, typer.Option("--api", help="API URL")] = "",
) -> None:
    """Preserve every input column and append the new QA response."""
    try:
        input_columns, rows = _read_case_rows(cases)
    except (OSError, UnicodeError, ValueError) as exc:
        console.print(f"[red]Invalid cases CSV:[/red] {exc}")
        raise typer.Exit(1) from exc

    api_url = (api_url or get_api_url()).rstrip("/")
    require_api_key(api_url)
    try:
        response = httpx.get(
            f"{api_url}/qa-evals/{experiment_id}",
            timeout=120.0,
            headers=get_auth_headers(api_url),
        )
    except httpx.RequestError as exc:
        console.print(f"[red]Failed to connect to API:[/red] {exc}")
        raise typer.Exit(1) from exc
    if response.status_code != 200:
        console.print(
            f"[red]Failed to collect QA evaluation:[/red] "
            f"{response.status_code} - {response.text}"
        )
        raise typer.Exit(1)

    result_by_source = {
        result["source_trial_id"]: result for result in response.json()["rows"]
    }
    output_rows = []
    for source_row in rows:
        source_id = source_row["source_trial_id"].strip()
        result = result_by_source.get(source_id)
        analysis = (result or {}).get("analysis") or {}
        output_rows.append(
            {
                **source_row,
                "new_qa_trial_id": (result or {}).get("qa_eval_trial_id", ""),
                "new_qa_status": (result or {}).get("status", ""),
                "new_qa_analysis_status": (result or {}).get("analysis_status", ""),
                "new_qa_classification": analysis.get("classification", ""),
                "new_qa_subtype": analysis.get("subtype", ""),
                "new_qa_evidence": analysis.get("evidence", ""),
                "new_qa_root_cause": analysis.get("root_cause", ""),
                "new_qa_recommendation": analysis.get("recommendation", ""),
                "new_qa_action_items_json": json.dumps(
                    analysis.get("action_items") or [],
                    ensure_ascii=False,
                    sort_keys=True,
                ),
                "new_qa_exploitation_json": json.dumps(
                    analysis.get("exploitation") or [],
                    ensure_ascii=False,
                    sort_keys=True,
                ),
                "new_qa_output_json": json.dumps(
                    analysis, ensure_ascii=False, sort_keys=True
                ),
                "new_qa_error": (
                    (result or {}).get("analysis_error")
                    or ("no replay result returned" if result is None else "")
                ),
                "prompt_name": (result or {}).get("prompt_name", ""),
                "prompt_sha256": (result or {}).get("prompt_sha256", ""),
                "qa_model": (result or {}).get("model", ""),
            }
        )

    output_columns = [
        column for column in input_columns if column not in _NEW_RESULT_COLUMNS
    ] + _NEW_RESULT_COLUMNS
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=output_columns)
        writer.writeheader()
        writer.writerows(output_rows)
    console.print(f"[green]Wrote[/green] {len(output_rows)} row(s) to {out}")
