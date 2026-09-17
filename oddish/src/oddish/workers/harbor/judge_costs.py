"""Reward Kit agent-judge usage, separate from the solving agent's spend."""

from __future__ import annotations

import hashlib
import json
import math
from itertools import islice
from pathlib import Path
from typing import Any

import tomllib
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from oddish.core.trial_artifacts import trial_name_from_manifest
from oddish.db.models import AnalysisCostModel, TrialModel
from oddish.model_pricing import estimate_cost_usd, has_pricing

_MAX_BYTES = 2 * 1024 * 1024
_MAX_COMPONENTS = 128
_TOKEN_FIELDS = (
    "input_tokens",
    "output_tokens",
    "cache_read_input_tokens",
    "cache_creation_input_tokens",
)


def judge_costs_enabled(task_path: Path) -> bool:
    """Read the task's opt-in before any verifier can spend money."""
    with (task_path / "task.toml").open("rb") as source:
        config = tomllib.loads(source.read(_MAX_BYTES + 1).decode())
    metadata = config.get("metadata", {})
    oddish = metadata.get("oddish", {}) if isinstance(metadata, dict) else {}
    return isinstance(oddish, dict) and oddish.get("verifier_judge_costs") is True


def _identity(trial_id: str, attempt: int, component: str, model: str) -> str:
    key = json.dumps(["verifier_judge", trial_id, attempt, component, model])
    return hashlib.sha256(key.encode()).hexdigest()


def _row(trial: TrialModel, attempt: int, component: str, model: str) -> dict:
    return {
        "id": _identity(trial.id, attempt, component, model),
        "job_kind": "verifier_judge",
        "trial_id": trial.id,
        "task_id": trial.task_id,
        "experiment_id": trial.experiment_id,
        "org_id": trial.org_id,
        "billed_user_id": trial.billed_user_id,
        "model": model or None,
        "cost_source": "estimated",
        "cost_usd": None,
    }


async def begin_judge_costs(
    session: AsyncSession, trial: TrialModel, attempt: int
) -> None:
    """Caller holds the trial lock and has verified the current worker/attempt."""
    row = _row(trial, attempt, "$accounting", "")
    row["cost_source"] = "pending"
    await session.execute(
        insert(AnalysisCostModel).values(row).on_conflict_do_nothing()
    )
    trial.result = {
        **(trial.result or {}),
        "_verifier_judges": {"status": "pending", "attempt": attempt},
    }


def _tokens(usage: Any) -> dict[str, int]:
    if not isinstance(usage, dict):
        raise TypeError("missing_usage")
    result = {}
    for field in _TOKEN_FIELDS:
        value = usage.get(field, 0 if field.startswith("cache_") else None)
        if type(value) is not int or not 0 <= value <= 2_147_483_647:
            raise ValueError("invalid_usage")
        result[field] = value
    if (
        result["cache_read_input_tokens"] + result["cache_creation_input_tokens"]
        > result["input_tokens"]
    ):
        raise ValueError("invalid_cache_usage")
    return result


def _has_unpriced_tools(usage: dict) -> bool:
    tools = usage.get("tool_requests", {})
    return not isinstance(tools, dict) or any(
        type(value) is not int or value != 0 for value in tools.values()
    )


def _components(payload: Any, path: tuple[str, ...] = ()):
    if not isinstance(payload, dict) or len(path) > 12:
        raise ValueError("invalid_components")
    if not path:
        for name, child in payload.items():
            yield from _components(child, (name,))
    elif payload.get("kind") == "group":
        children = payload.get("components")
        if not isinstance(children, list) or not children:
            raise ValueError("invalid_components")
        names = set()
        for child in children:
            if not isinstance(child, dict):
                raise TypeError("invalid_components")
            name = child.get("name")
            if not isinstance(name, str) or name in names:
                raise ValueError("invalid_components")
            names.add(name)
            yield from _components(child.get("detail"), (*path, name))
    elif payload.get("kind") in ("agent", "llm"):
        yield json.dumps(path, ensure_ascii=True), payload
    elif payload.get("kind") != "programmatic":
        raise ValueError("unknown_component_kind")


def _read_report(job_dir: Path | None, result_path: Path | None) -> tuple[dict, str]:
    if job_dir is None or result_path is None:
        raise ValueError("missing_report")
    # Select the same Harbor child as artifact readers. Never scan unrelated
    # trial directories, follow a task-supplied path, or read a symlink outside it.
    with result_path.open("rb") as source:
        manifest = json.loads(source.read(_MAX_BYTES + 1))
    name = trial_name_from_manifest(manifest)
    if name is None:
        raise ValueError("missing_trial_manifest")
    report = job_dir / name / "verifier" / "reward-details.json"
    if not report.resolve().is_relative_to(job_dir.resolve()):
        raise ValueError("invalid_report_path")
    with report.open("rb") as source:
        document = source.read(_MAX_BYTES + 1)
    if len(document) > _MAX_BYTES:
        raise ValueError("report_too_large")
    payload = json.loads(document)
    return payload, hashlib.sha256(document).hexdigest()


def extract_judge_costs(job_dir: Path | None, result_path: Path | None) -> dict:
    """Return bounded token rows and fixed error codes; never persist prompts."""
    rows, errors = [], set()
    digest = None
    try:
        payload, digest = _read_report(job_dir, result_path)
        components = list(islice(_components(payload), _MAX_COMPONENTS + 1))
        if not payload or len(components) > _MAX_COMPONENTS:
            raise ValueError("invalid_component_count")
        for component, detail in components:
            if detail["kind"] != "agent":
                errors.add("unsupported_llm_judge")
                continue
            try:
                usage = detail.get("usage")
                totals = _tokens(usage)
                models = usage.get("models")
                # Never charge all tokens to the configured model when the
                # backend did not identify the models it actually used.
                if not isinstance(models, dict) or not 1 <= len(models) <= 16:
                    raise ValueError("missing_model_usage")
                if _has_unpriced_tools(usage):
                    errors.add("unpriced_tool_requests")
                folded = dict.fromkeys(_TOKEN_FIELDS, 0)
                for model, model_usage in models.items():
                    if not isinstance(model, str) or not 1 <= len(model) <= 128:
                        raise ValueError("invalid_model")
                    tokens = _tokens(model_usage)
                    for field, value in tokens.items():
                        folded[field] += value
                    if _has_unpriced_tools(model_usage):
                        errors.add("unpriced_tool_requests")
                    cost = estimate_cost_usd(
                        model, *(tokens[field] for field in _TOKEN_FIELDS)
                    )
                    if not any(tokens.values()) and has_pricing(model):
                        cost = 0.0
                    if cost is None or not math.isfinite(cost) or cost < 0:
                        cost = None
                        errors.add("unpriced_model")
                    rows.append(
                        {
                            "component": component,
                            "model": model,
                            "tokens": tokens,
                            "cost_usd": cost,
                        }
                    )
                if folded != totals:
                    errors.add("model_usage_mismatch")
            except (TypeError, ValueError) as exc:
                errors.add(str(exc))
    except (OSError, UnicodeError, TypeError, ValueError, RecursionError) as exc:
        code = str(exc)
        allowed = {
            "missing_report",
            "missing_trial_manifest",
            "invalid_report_path",
            "report_too_large",
            "invalid_component_count",
            "invalid_components",
            "unknown_component_kind",
        }
        errors.add(code if code in allowed else "unreadable_report")
    return {"rows": rows, "errors": sorted(errors), "sha256": digest}


async def settle_judge_costs(
    session: AsyncSession, trial: TrialModel, attempt: int, extracted: dict
) -> dict | None:
    """Settle one declared attempt under its verified trial ownership lock."""
    marker = await session.scalar(
        select(AnalysisCostModel)
        .where(AnalysisCostModel.id == _identity(trial.id, attempt, "$accounting", ""))
        .with_for_update()
    )
    if marker is None:
        return None
    if marker.cost_source != "pending":
        # Repeated settlement must neither charge again nor replace its evidence.
        return (trial.result or {}).get("_verifier_judges")
    for item in extracted["rows"]:
        row = _row(trial, attempt, item["component"], item["model"])
        tokens = item["tokens"]
        row.update(
            input_tokens=tokens["input_tokens"],
            output_tokens=tokens["output_tokens"],
            cache_read_tokens=tokens["cache_read_input_tokens"],
            cache_write_tokens=tokens["cache_creation_input_tokens"],
            cost_usd=item["cost_usd"],
        )
        await session.execute(
            insert(AnalysisCostModel).values(row).on_conflict_do_nothing()
        )
    complete = not extracted["errors"]
    marker.cost_source = "estimated"
    marker.cost_usd = 0.0 if complete else None
    return {
        "status": "recorded" if complete else "incomplete",
        "attempt": attempt,
        "errors": extracted["errors"],
        "components": len(extracted["rows"]),
        "report_sha256": extracted["sha256"],
        "report_path": "verifier/reward-details.json",
    }
