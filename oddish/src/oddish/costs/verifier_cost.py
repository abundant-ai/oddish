"""CUA / LLM verifier spend: extract artifacts and persist ``verifier_costs``.

Distinct from ``trials.cost_usd`` (solver) and ``analysis_costs`` (QA). Covers:

* Harbor ``type = "cua"`` (artifacts under ``verifier/``)
* SWE-Marathon-style inline CUA (``verifier/ux/``, ``cua_judge_report.json``)

Route prefers the **credential that actually bills**, not the Bedrock-looking
model spelling Oddish sometimes stores: SWE-M and force-direct Claude Code
spend hit ``ANTHROPIC_API_KEY`` / the Claude console.
"""

from __future__ import annotations

import json
import logging
import math
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from oddish.core.llm_key_fingerprint import platform_key_hash_for_provider
from oddish.db import VerifierCostModel, generate_id, get_session, utcnow
from oddish.model_pricing import estimate_cost_usd

log = logging.getLogger(__name__)

COMPONENT_LOOP = "cua_loop"
COMPONENT_JUDGE = "cua_judge"

ROUTE_ANTHROPIC = "anthropic"
ROUTE_BEDROCK = "bedrock"
ROUTE_OTHER = "other"

COST_NATIVE = "native"
COST_ESTIMATED = "estimated"
COST_BACKFILL = "backfill"

UNPRICED_MISSING_TRAJECTORY = "missing_trajectory"
UNPRICED_MISSING_USAGE = "missing_usage"

# Conservative per-criterion floor when the judge response has no usage.
_JUDGE_INPUT_TOKENS_PER_CRITERION = 2_000
_JUDGE_OUTPUT_TOKENS_PER_CRITERION = 300

_JUDGE_REPORT_NAMES = ("cua_judge_report.json",)
_TRAJECTORY_NAMES = ("trajectory.json",)


@dataclass(frozen=True)
class VerifierCostDraft:
    component: str
    model: str | None
    route: str
    llm_key_hash: str | None
    input_tokens: int | None
    output_tokens: int | None
    cache_read_tokens: int | None
    cache_write_tokens: int | None
    cost_usd: float | None
    cost_source: str
    unpriced_reason: str | None = None


@dataclass(frozen=True)
class CuaArtifactBundle:
    """Resolved CUA artifacts for one trial attempt."""

    verifier_dir: Path
    trajectory_path: Path | None
    judge_report_path: Path | None
    judge_report: dict[str, Any] | None
    loop_model: str | None
    judge_model: str | None


def infer_verifier_route(model: str | None) -> str:
    """Map a verifier model id to a billing recon bucket.

    ``anthropic/…`` and bare Claude ids → Anthropic (Claude console).
    Explicit ``bedrock/…`` → Bedrock (AWS), even though Oddish often *labels*
    Claude Code as Bedrock while force-direct bills Anthropic — CUA configs
    that mean Anthropic use the ``anthropic/`` prefix (SWE-M).
    """
    raw = (model or "").strip().lower()
    if not raw:
        return ROUTE_OTHER
    if raw.startswith("bedrock/") or raw.startswith("bedrock."):
        return ROUTE_BEDROCK
    if (
        raw.startswith("anthropic/")
        or raw.startswith("claude")
        or "/claude" in raw
        or raw.startswith("us.anthropic.")
        or raw.startswith("global.anthropic.")
    ):
        # us.anthropic.* without bedrock/ is still often an Anthropic-shaped
        # id that force-direct rewrote; attribute to Anthropic for console recon
        # when not explicitly bedrock-prefixed.
        if raw.startswith("us.anthropic.") or raw.startswith("global.anthropic."):
            # These look like Bedrock inference-profile ids. Prefer Anthropic
            # when the platform Anthropic key is present (force-direct reality).
            if os.environ.get("ANTHROPIC_API_KEY", "").strip():
                return ROUTE_ANTHROPIC
            return ROUTE_BEDROCK
        return ROUTE_ANTHROPIC
    return ROUTE_OTHER


def _key_hash_for_route(route: str) -> str | None:
    if route == ROUTE_ANTHROPIC:
        return platform_key_hash_for_provider("anthropic")
    if route == ROUTE_BEDROCK:
        return platform_key_hash_for_provider("bedrock")
    return None


def _as_int(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _as_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        n = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(n) or n < 0:
        return None
    return n


def _load_json(path: Path) -> dict[str, Any] | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def _metrics_from_atif(data: dict[str, Any]) -> tuple[
    int | None, int | None, int | None, int | None, float | None
]:
    """Return input, output, cache_read, cache_write, cost from an ATIF object."""
    final_metrics = data.get("final_metrics")
    input_tokens = output_tokens = cache_tokens = cache_write = cost = None
    if isinstance(final_metrics, dict):
        input_tokens = _as_int(final_metrics.get("total_prompt_tokens"))
        output_tokens = _as_int(final_metrics.get("total_completion_tokens"))
        cache_tokens = _as_int(final_metrics.get("total_cached_tokens"))
        cost = _as_float(final_metrics.get("total_cost_usd"))
        extra = final_metrics.get("extra")
        if isinstance(extra, dict):
            for key in (
                "cache_creation_input_tokens",
                "input_cache_creation",
                "cacheWriteTokens",
                "cache_write_tokens",
            ):
                if extra.get(key) is not None:
                    cache_write = _as_int(extra.get(key))
                    break
    return input_tokens, output_tokens, cache_tokens, cache_write, cost


def find_cua_artifact_dirs(job_dir: Path) -> list[Path]:
    """Directories that look like a CUA verifier output root."""
    if not job_dir or not job_dir.exists():
        return []
    found: list[Path] = []
    seen: set[Path] = set()
    for name in _JUDGE_REPORT_NAMES:
        for report in sorted(job_dir.rglob(name)):
            parent = report.parent.resolve()
            if parent in seen:
                continue
            seen.add(parent)
            found.append(parent)
    if found:
        return found
    # Trajectory under verifier/ without a judge report (partial run).
    for traj in sorted(job_dir.rglob("trajectory.json")):
        parts = {p.lower() for p in traj.parts}
        if "verifier" not in parts:
            continue
        parent = traj.parent.resolve()
        if parent in seen:
            continue
        seen.add(parent)
        found.append(parent)
    return found


def resolve_cua_bundle(job_dir: Path, task_path: Path | None = None) -> CuaArtifactBundle | None:
    """Locate CUA artifacts under a Harbor job directory."""
    dirs = find_cua_artifact_dirs(job_dir)
    if not dirs and task_path is not None and not task_has_cua_signals(task_path):
        return None
    if not dirs:
        # Task claims CUA but artifacts missing — still return a stub dir for
        # unpriced rows when the caller knows this attempt ran a CUA stage.
        return None

    verifier_dir = dirs[0]
    judge_path = None
    for name in _JUDGE_REPORT_NAMES:
        candidate = verifier_dir / name
        if candidate.is_file():
            judge_path = candidate
            break
    judge_report = _load_json(judge_path) if judge_path else None

    traj_path = None
    for name in _TRAJECTORY_NAMES:
        candidate = verifier_dir / name
        if candidate.is_file():
            traj_path = candidate
            break
    if traj_path is None:
        for candidate in sorted(verifier_dir.rglob("trajectory.json")):
            traj_path = candidate
            break

    loop_model = None
    judge_model = None
    if isinstance(judge_report, dict):
        loop_model = (
            str(judge_report.get("verifier_model") or "").strip() or None
        )
        judge_model = str(judge_report.get("judge_model") or "").strip() or None
    if loop_model is None and task_path is not None:
        cfg = load_cua_model_config(task_path)
        loop_model = cfg.get("model")
        judge_model = judge_model or cfg.get("judge_model")
    if judge_model is None:
        judge_model = loop_model

    return CuaArtifactBundle(
        verifier_dir=verifier_dir,
        trajectory_path=traj_path,
        judge_report_path=judge_path,
        judge_report=judge_report,
        loop_model=loop_model,
        judge_model=judge_model,
    )


def task_has_cua_signals(task_path: Path) -> bool:
    """True when the task bundle configures an inline or Harbor CUA verifier."""
    if not task_path or not task_path.exists():
        return False
    if (task_path / "tests" / "cua_config.json").is_file():
        return True
    if (task_path / "tests" / "cua_verifier.py").is_file():
        return True
    toml_path = task_path / "task.toml"
    if not toml_path.is_file():
        return False
    try:
        text = toml_path.read_text(encoding="utf-8")
    except OSError:
        return False
    lowered = text.lower()
    if re.search(r'type\s*=\s*["\']cua["\']', lowered):
        return True
    if "[verifier.cua]" in lowered or "[[verifiers]]" in lowered and 'type = "cua"' in lowered:
        return True
    return False


def load_cua_model_config(task_path: Path) -> dict[str, str | None]:
    """Best-effort model / judge_model from cua_config.json or task.toml."""
    out: dict[str, str | None] = {"model": None, "judge_model": None}
    cfg_path = task_path / "tests" / "cua_config.json"
    if cfg_path.is_file():
        data = _load_json(cfg_path)
        if data:
            model = data.get("model")
            judge = data.get("judge_model")
            out["model"] = str(model).strip() if model else None
            out["judge_model"] = str(judge).strip() if judge else out["model"]
            return out
    toml_path = task_path / "task.toml"
    if not toml_path.is_file():
        return out
    try:
        text = toml_path.read_text(encoding="utf-8")
    except OSError:
        return out
    # Minimal parse: look for model = "..." near a cua section.
    m = re.search(
        r"\[verifier\.cua\][^\[]*?model\s*=\s*[\"']([^\"']+)[\"']",
        text,
        re.IGNORECASE | re.DOTALL,
    )
    if m:
        out["model"] = m.group(1).strip()
    j = re.search(
        r"\[verifier\.cua\][^\[]*?judge_model\s*=\s*[\"']([^\"']+)[\"']",
        text,
        re.IGNORECASE | re.DOTALL,
    )
    if j:
        out["judge_model"] = j.group(1).strip()
    if out["judge_model"] is None:
        out["judge_model"] = out["model"]
    return out


def draft_loop_cost(bundle: CuaArtifactBundle) -> VerifierCostDraft:
    model = bundle.loop_model
    route = infer_verifier_route(model)
    key_hash = _key_hash_for_route(route)
    if bundle.trajectory_path is None or not bundle.trajectory_path.is_file():
        return VerifierCostDraft(
            component=COMPONENT_LOOP,
            model=model,
            route=route,
            llm_key_hash=key_hash,
            input_tokens=None,
            output_tokens=None,
            cache_read_tokens=None,
            cache_write_tokens=None,
            cost_usd=None,
            cost_source=COST_ESTIMATED,
            unpriced_reason=UNPRICED_MISSING_TRAJECTORY,
        )
    data = _load_json(bundle.trajectory_path)
    if not data:
        return VerifierCostDraft(
            component=COMPONENT_LOOP,
            model=model,
            route=route,
            llm_key_hash=key_hash,
            input_tokens=None,
            output_tokens=None,
            cache_read_tokens=None,
            cache_write_tokens=None,
            cost_usd=None,
            cost_source=COST_ESTIMATED,
            unpriced_reason=UNPRICED_MISSING_TRAJECTORY,
        )
    inp, out, cache, cache_write, cost = _metrics_from_atif(data)
    if cost is not None and cost > 0:
        return VerifierCostDraft(
            component=COMPONENT_LOOP,
            model=model,
            route=route,
            llm_key_hash=key_hash,
            input_tokens=inp,
            output_tokens=out,
            cache_read_tokens=cache,
            cache_write_tokens=cache_write,
            cost_usd=cost,
            cost_source=COST_NATIVE,
        )
    estimated = estimate_cost_usd(model, inp, out, cache, cache_write)
    if estimated is not None:
        return VerifierCostDraft(
            component=COMPONENT_LOOP,
            model=model,
            route=route,
            llm_key_hash=key_hash,
            input_tokens=inp,
            output_tokens=out,
            cache_read_tokens=cache,
            cache_write_tokens=cache_write,
            cost_usd=estimated,
            cost_source=COST_ESTIMATED,
        )
    return VerifierCostDraft(
        component=COMPONENT_LOOP,
        model=model,
        route=route,
        llm_key_hash=key_hash,
        input_tokens=inp,
        output_tokens=out,
        cache_read_tokens=cache,
        cache_write_tokens=cache_write,
        cost_usd=None,
        cost_source=COST_ESTIMATED,
        unpriced_reason=UNPRICED_MISSING_USAGE,
    )


def draft_judge_cost(bundle: CuaArtifactBundle) -> VerifierCostDraft | None:
    report = bundle.judge_report
    if not isinstance(report, dict):
        return None
    model = bundle.judge_model or bundle.loop_model
    route = infer_verifier_route(model)
    key_hash = _key_hash_for_route(route)
    verdicts = report.get("verdicts")
    criteria_count = len(verdicts) if isinstance(verdicts, list) else 0
    if criteria_count <= 0:
        criteria_count = 1
    inp = _JUDGE_INPUT_TOKENS_PER_CRITERION * criteria_count
    out = _JUDGE_OUTPUT_TOKENS_PER_CRITERION * criteria_count
    estimated = estimate_cost_usd(model, inp, out, 0, 0)
    return VerifierCostDraft(
        component=COMPONENT_JUDGE,
        model=model,
        route=route,
        llm_key_hash=key_hash,
        input_tokens=inp,
        output_tokens=out,
        cache_read_tokens=0,
        cache_write_tokens=0,
        cost_usd=estimated,
        cost_source=COST_ESTIMATED,
        unpriced_reason=None if estimated is not None else UNPRICED_MISSING_USAGE,
    )


def build_verifier_cost_drafts(
    job_dir: Path, task_path: Path | None = None
) -> list[VerifierCostDraft]:
    """Return loop (+ judge) drafts when CUA artifacts are present."""
    bundle = resolve_cua_bundle(job_dir, task_path=task_path)
    if bundle is None:
        return []
    drafts = [draft_loop_cost(bundle)]
    judge = draft_judge_cost(bundle)
    if judge is not None:
        drafts.append(judge)
    return drafts


async def upsert_verifier_cost_rows(
    session: AsyncSession,
    *,
    drafts: list[VerifierCostDraft],
    trial_id: str,
    attempt: int,
    experiment_id: str | None,
    org_id: str | None,
    task_id: str | None,
    task_version_id: str | None,
    cost_source_override: str | None = None,
) -> int:
    """Insert or no-op existing ``(trial_id, attempt, component)`` rows.

    Never updates an existing live row — failed attempts keep their spend;
    a later SUCCESS does not overwrite. Returns the number of rows inserted.
    """
    if not drafts:
        return 0
    inserted = 0
    now = utcnow()
    for draft in drafts:
        source = cost_source_override or draft.cost_source
        values = {
            "id": generate_id(),
            "trial_id": trial_id,
            "attempt": attempt,
            "component": draft.component,
            "experiment_id": experiment_id,
            "org_id": org_id,
            "billed_user_id": None,
            "task_id": task_id,
            "task_version_id": task_version_id,
            "model": draft.model,
            "route": draft.route,
            "llm_key_hash": draft.llm_key_hash,
            "input_tokens": draft.input_tokens,
            "output_tokens": draft.output_tokens,
            "cache_read_tokens": draft.cache_read_tokens,
            "cache_write_tokens": draft.cache_write_tokens,
            "cost_usd": draft.cost_usd,
            "cost_source": source,
            "unpriced_reason": draft.unpriced_reason,
            "created_at": now,
            "updated_at": now,
            "deleted_at": None,
        }
        stmt = (
            pg_insert(VerifierCostModel)
            .values(**values)
            .on_conflict_do_nothing(
                index_elements=["trial_id", "attempt", "component"],
                index_where=VerifierCostModel.deleted_at.is_(None),
            )
        )
        result = await session.execute(stmt)
        # rowcount is 1 on insert, 0 on conflict (dialect-dependent; treat None as 0)
        if result.rowcount and result.rowcount > 0:
            inserted += 1
    return inserted


async def record_verifier_llm_costs(
    *,
    job_dir: Path | None,
    task_path: Path | None,
    trial_id: str,
    attempt: int,
    experiment_id: str | None,
    org_id: str | None,
    task_id: str | None,
    task_version_id: str | None,
    cost_source_override: str | None = None,
) -> int:
    """Best-effort settlement write. Never raises into the trial path."""
    if job_dir is None:
        return 0
    try:
        drafts = build_verifier_cost_drafts(job_dir, task_path=task_path)
        if not drafts:
            return 0
        async with get_session() as session:
            n = await upsert_verifier_cost_rows(
                session,
                drafts=drafts,
                trial_id=trial_id,
                attempt=attempt,
                experiment_id=experiment_id,
                org_id=org_id,
                task_id=task_id,
                task_version_id=task_version_id,
                cost_source_override=cost_source_override,
            )
        return n
    except Exception:
        log.exception(
            "verifier LLM cost settlement failed trial_id=%s attempt=%s",
            trial_id,
            attempt,
        )
        return 0


async def existing_verifier_cost_keys(
    session: AsyncSession, trial_ids: list[str]
) -> set[tuple[str, int]]:
    """``(trial_id, attempt)`` pairs that already have at least one ledger row."""
    if not trial_ids:
        return set()
    rows = (
        await session.execute(
            select(VerifierCostModel.trial_id, VerifierCostModel.attempt).where(
                VerifierCostModel.deleted_at.is_(None),
                VerifierCostModel.trial_id.in_(trial_ids),
            )
        )
    ).all()
    return {(row.trial_id, int(row.attempt)) for row in rows}


_BACKFILL_BATCH = 200

# Attempt-relative keys to try (Harbor CUA + SWE-M ux layout).
_BACKFILL_ARTIFACT_CANDIDATES = (
    ("verifier/ux/cua_judge_report.json", "cua_judge_report.json"),
    ("verifier/cua_judge_report.json", "cua_judge_report.json"),
    ("verifier/ux/trajectory.json", "trajectory.json"),
    ("verifier/trajectory.json", "trajectory.json"),
)


async def backfill_verifier_costs_from_s3(*, limit: int = _BACKFILL_BATCH) -> int:
    """Best-effort historical CUA spend import. Cap ``limit`` trials per call.

    Selects finished agent trials with an S3 attempt prefix and no
    ``verifier_costs`` row for that attempt, downloads CUA artifacts when
    present, and inserts with ``cost_source=backfill``. Missing artifacts
    skip the trial (retry next sweep). Never invents duration-based prices.
    """
    import tempfile

    from oddish.core.trial_artifacts import (
        TrialArtifactMode,
        resolve_trial_artifact_layout,
    )
    from oddish.db import TrialModel, TrialStatus
    from oddish.db.storage import StorageClient, is_missing_object

    try:
        from botocore.exceptions import ClientError
    except ImportError:  # pragma: no cover
        ClientError = Exception  # type: ignore[misc, assignment]

    async with get_session() as session:
        # Anti-join: finished agent trials with S3 key and no ledger row for
        # the current attempt. Prefer recent finishes so live recon catches up.
        existing = (
            select(VerifierCostModel.trial_id, VerifierCostModel.attempt)
            .where(VerifierCostModel.deleted_at.is_(None))
            .subquery()
        )
        candidates = (
            await session.execute(
                select(TrialModel)
                .outerjoin(
                    existing,
                    (existing.c.trial_id == TrialModel.id)
                    & (existing.c.attempt == TrialModel.attempts),
                )
                .where(
                    TrialModel.kind == "agent",
                    TrialModel.finished_at.isnot(None),
                    TrialModel.trial_s3_key.isnot(None),
                    TrialModel.status.in_(
                        (
                            TrialStatus.SUCCESS,
                            TrialStatus.FAILED,
                            TrialStatus.SKIPPED,
                        )
                    ),
                    existing.c.trial_id.is_(None),
                )
                .order_by(TrialModel.finished_at.desc())
                .limit(limit)
            )
        ).scalars().all()

    if not candidates:
        return 0

    storage = StorageClient()
    inserted_total = 0
    for trial in candidates:
        try:
            layout = await resolve_trial_artifact_layout(trial, storage)
            if layout.mode is TrialArtifactMode.UNAVAILABLE or not layout.artifact_prefix:
                continue
            prefix = layout.artifact_prefix.rstrip("/") + "/"
            with tempfile.TemporaryDirectory(prefix="cua-backfill-") as tmp:
                tmp_path = Path(tmp)
                verifier_dir = tmp_path / "verifier" / "ux"
                verifier_dir.mkdir(parents=True, exist_ok=True)
                found_any = False
                for relative, local_name in _BACKFILL_ARTIFACT_CANDIDATES:
                    key = f"{prefix}{relative}"
                    try:
                        body = await storage.download_bytes(key)
                    except ClientError as exc:
                        if is_missing_object(exc):
                            continue
                        raise
                    except Exception:
                        # Missing-object helpers vary by backend; treat soft miss.
                        if not await storage.object_exists(key):
                            continue
                        raise
                    (verifier_dir / local_name).write_bytes(body)
                    found_any = True
                if not found_any:
                    continue
                n = await record_verifier_llm_costs(
                    job_dir=tmp_path,
                    task_path=None,
                    trial_id=trial.id,
                    attempt=int(trial.attempts or 1),
                    experiment_id=trial.experiment_id,
                    org_id=trial.org_id,
                    task_id=trial.task_id,
                    task_version_id=trial.task_version_id,
                    cost_source_override=COST_BACKFILL,
                )
                inserted_total += n
        except Exception:
            log.exception(
                "verifier cost backfill failed trial_id=%s",
                trial.id,
            )
            continue
    return inserted_total
