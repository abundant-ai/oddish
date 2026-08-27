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

_COLLECT_COLUMNS = [
    "source_trial_id",
    "qa_eval_trial_id",
    "task_name",
    "researcher_issue",
    "prompt_name",
    "prompt_sha256",
    "model",
    "historical_qa_response_valid",
    "historical_qa_classification",
    "historical_qa_root_cause",
    "candidate_qa_classification",
    "candidate_qa_subtype",
    "candidate_qa_evidence",
    "candidate_qa_root_cause",
    "candidate_qa_recommendation",
    "candidate_qa_action_items_json",
    "candidate_qa_exploitation_json",
    "candidate_qa_output_json",
    "researcher_issue_caught",
    "qa_response_valid",
    "failure_stage",
]


def _read_cases(path: Path) -> list[str]:
    with path.open(newline="", encoding="utf-8-sig") as handle:
        reader = csv.DictReader(handle)
        if not reader.fieldnames or "source_trial_id" not in reader.fieldnames:
            raise ValueError("cases CSV must contain a source_trial_id column")
        values = [str(row.get("source_trial_id") or "").strip() for row in reader]
    trial_ids = list(dict.fromkeys(value for value in values if value))
    if not trial_ids:
        raise ValueError("cases CSV contains no source_trial_id values")
    return trial_ids


def _read_prompt_spec(spec: str) -> tuple[str, str]:
    prompt_name, separator, raw_path = spec.partition("=")
    prompt_name = prompt_name.strip()
    raw_path = raw_path.strip()
    if not separator or not prompt_name or not raw_path:
        raise ValueError(
            "--prompt must use NAME=PATH, for example candidate-1=prompt.txt"
        )
    path = Path(raw_path).expanduser()
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
    if count == 1:
        return base
    return f"{base}-{prompt_name}"


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
            help="Candidate prompt as NAME=PATH. Repeat to queue several prompts.",
        ),
    ] = None,
    name: Annotated[
        Optional[str],
        typer.Option(
            "--name",
            help="Output experiment name; prompt name is appended for multiple prompts.",
        ),
    ] = None,
    model: Annotated[
        Optional[str],
        typer.Option(
            "--model",
            help="QA model override; omitted uses the deployed production QA model.",
        ),
    ] = None,
    api_url: Annotated[str, typer.Option("--api", help="API URL")] = "",
    json_output: Annotated[
        bool, typer.Option("--json", help="Output machine-readable JSON.")
    ] = False,
) -> None:
    """Queue candidate QA prompts without downloading solver artifacts."""
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
    results: list[dict] = []
    failures: list[dict] = []
    with httpx.Client(timeout=120.0, headers=get_auth_headers(api_url)) as client:
        for prompt_name, prompt_text in parsed_prompts:
            experiment_name = _experiment_name(name, prompt_name, len(parsed_prompts))
            payload = QAEvalCreateRequest(
                name=experiment_name,
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
            results.append(response.json())

    output = {"queued": results, "failed": failures}
    if json_output:
        print_json(output)
    else:
        for result in results:
            console.print(
                f"[green]Queued[/green] {result['queued_count']} replay(s); "
                f"skipped {result['skipped_count']} in "
                f"{result['experiment_name']} ({result['experiment_id']}) using "
                f"{result['model']}"
            )
        for failure in failures:
            console.print(
                f"[red]Failed[/red] {failure['prompt_name']}: "
                f"{failure.get('status', 'connection')} - {failure['error']}"
            )
    if failures:
        raise typer.Exit(1)


def _read_labels(path: Path) -> dict[str, dict[str, str]]:
    with path.open(newline="", encoding="utf-8-sig") as handle:
        reader = csv.DictReader(handle)
        if not reader.fieldnames or "source_trial_id" not in reader.fieldnames:
            raise ValueError("labels CSV must contain a source_trial_id column")
        return {
            source_id: {key: str(value or "") for key, value in row.items()}
            for row in reader
            if (source_id := str(row.get("source_trial_id") or "").strip())
        }


@qa_eval_app.command("collect")
def collect_qa_eval(
    experiment: Annotated[
        str, typer.Argument(help="QA-eval experiment ID or exact experiment name.")
    ],
    labels: Annotated[
        Path,
        typer.Option(
            "--labels",
            exists=True,
            dir_okay=False,
            readable=True,
            help="Researcher-label CSV keyed by source_trial_id.",
        ),
    ],
    out: Annotated[Path, typer.Option("--out", help="Comparison CSV path.")],
    api_url: Annotated[str, typer.Option("--api", help="API URL")] = "",
) -> None:
    """Write historical and candidate QA results beside researcher labels."""
    try:
        label_by_id = _read_labels(labels)
    except (OSError, UnicodeError, ValueError) as exc:
        console.print(f"[red]Invalid labels CSV:[/red] {exc}")
        raise typer.Exit(1) from exc

    api_url = (api_url or get_api_url()).rstrip("/")
    require_api_key(api_url)
    try:
        response = httpx.get(
            f"{api_url}/qa-evals/{experiment}",
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

    rows: list[dict[str, str]] = []
    for result in response.json()["rows"]:
        label = label_by_id.get(result["source_trial_id"], {})
        rows.append(
            {
                "source_trial_id": result["source_trial_id"],
                "qa_eval_trial_id": result["qa_eval_trial_id"],
                "task_name": result["task_name"],
                "researcher_issue": label.get("researcher_issue", ""),
                "prompt_name": result["prompt_name"],
                "prompt_sha256": result["prompt_sha256"],
                "model": result["model"],
                "historical_qa_response_valid": str(
                    result["historical_qa_response_valid"]
                ).lower(),
                "historical_qa_classification": result.get(
                    "historical_qa_classification"
                )
                or "",
                "historical_qa_root_cause": result.get("historical_qa_root_cause")
                or "",
                "candidate_qa_classification": result.get("candidate_qa_classification")
                or "",
                "candidate_qa_subtype": result.get("candidate_qa_subtype") or "",
                "candidate_qa_evidence": result.get("candidate_qa_evidence") or "",
                "candidate_qa_root_cause": result.get("candidate_qa_root_cause") or "",
                "candidate_qa_recommendation": result.get("candidate_qa_recommendation")
                or "",
                "candidate_qa_action_items_json": json.dumps(
                    result.get("candidate_qa_action_items") or [],
                    ensure_ascii=False,
                    sort_keys=True,
                ),
                "candidate_qa_exploitation_json": json.dumps(
                    result.get("candidate_qa_exploitation") or [],
                    ensure_ascii=False,
                    sort_keys=True,
                ),
                "candidate_qa_output_json": json.dumps(
                    result.get("candidate_qa_output") or {},
                    ensure_ascii=False,
                    sort_keys=True,
                ),
                # The replay never sees researcher_issue, so this remains a
                # researcher label unless the input CSV already supplies it.
                "researcher_issue_caught": label.get("researcher_issue_caught", ""),
                "qa_response_valid": str(result["qa_response_valid"]).lower(),
                "failure_stage": result.get("failure_stage") or "",
            }
        )

    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=_COLLECT_COLUMNS)
        writer.writeheader()
        writer.writerows(rows)
    console.print(f"[green]Wrote[/green] {len(rows)} comparison row(s) to {out}")
