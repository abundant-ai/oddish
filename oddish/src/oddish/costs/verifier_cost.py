"""CUA / LLM verifier spend: extract artifacts and persist ``verifier_costs``.

Distinct from ``trials.cost_usd`` (solver) and ``analysis_costs`` (QA). Covers:

* Harbor ``type = "cua"`` (artifacts under ``verifier/``)
* SWE-Marathon-style inline CUA (``verifier/ux/``, ``cua_judge_report.json``)

Route uses the model id spelling: ``anthropic/…`` → Claude console;
``bedrock/…`` and Bedrock inference-profile ids → AWS. SWE-M CUA configs
use the ``anthropic/`` prefix on the platform ``ANTHROPIC_API_KEY``.
"""

from __future__ import annotations

import json
import logging
import math
import re
from dataclasses import dataclass
from datetime import datetime
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
# Known CUA output roots (Harbor + SWE-M). Prefer these over rglob.
_CUA_OUTPUT_RELS = ("verifier/ux", "verifier")
UNPRICED_NO_CUA_ARTIFACTS = "no_cua_artifacts"


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
    Explicit ``bedrock/…`` or Bedrock inference-profile ids
    (``us.anthropic.*`` / ``global.anthropic.*``) → Bedrock.
    """
    raw = (model or "").strip().lower()
    if not raw:
        return ROUTE_OTHER
    if (
        raw.startswith("bedrock/")
        or raw.startswith("bedrock.")
        or raw.startswith("us.anthropic.")
        or raw.startswith("global.anthropic.")
    ):
        return ROUTE_BEDROCK
    if (
        raw.startswith("anthropic/")
        or raw.startswith("claude")
        or "/claude" in raw
    ):
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
    """Directories that look like a CUA verifier output root.

    Harbor's job root holds a trial-name subdirectory; artifacts live under
    ``<job>/<trial_name>/verifier[/ux]``, not ``<job>/verifier``. Also accept
    a flattened tree (backfill / selected trial dir) at ``job_dir`` itself.
    """
    if not job_dir or not job_dir.exists():
        return []
    roots: list[Path] = [job_dir]
    try:
        roots.extend(p for p in sorted(job_dir.iterdir()) if p.is_dir())
    except OSError:
        pass
    found: list[Path] = []
    seen: set[Path] = set()
    for root in roots:
        for rel in _CUA_OUTPUT_RELS:
            directory = (root / rel).resolve()
            if directory in seen or not directory.is_dir():
                continue
            has_report = any(
                (directory / name).is_file() for name in _JUDGE_REPORT_NAMES
            )
            has_traj = any(
                (directory / name).is_file() for name in _TRAJECTORY_NAMES
            )
            if has_report or has_traj:
                seen.add(directory)
                found.append(directory)
    return found


def resolve_cua_bundle(job_dir: Path, task_path: Path | None = None) -> CuaArtifactBundle | None:
    """Locate CUA artifacts under a Harbor job directory."""
    dirs = find_cua_artifact_dirs(job_dir)
    if not dirs:
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


def _loop_draft(
    *,
    model: str | None,
    route: str,
    key_hash: str | None,
    input_tokens: int | None = None,
    output_tokens: int | None = None,
    cache_read_tokens: int | None = None,
    cache_write_tokens: int | None = None,
    cost_usd: float | None = None,
    cost_source: str = COST_ESTIMATED,
    unpriced_reason: str | None = None,
) -> VerifierCostDraft:
    return VerifierCostDraft(
        component=COMPONENT_LOOP,
        model=model,
        route=route,
        llm_key_hash=key_hash,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cache_read_tokens=cache_read_tokens,
        cache_write_tokens=cache_write_tokens,
        cost_usd=cost_usd,
        cost_source=cost_source,
        unpriced_reason=unpriced_reason,
    )


def draft_loop_cost(bundle: CuaArtifactBundle) -> VerifierCostDraft:
    model = bundle.loop_model
    route = infer_verifier_route(model)
    key_hash = _key_hash_for_route(route)
    if bundle.trajectory_path is None or not bundle.trajectory_path.is_file():
        return _loop_draft(
            model=model,
            route=route,
            key_hash=key_hash,
            unpriced_reason=UNPRICED_MISSING_TRAJECTORY,
        )
    data = _load_json(bundle.trajectory_path)
    if not data:
        return _loop_draft(
            model=model,
            route=route,
            key_hash=key_hash,
            unpriced_reason=UNPRICED_MISSING_TRAJECTORY,
        )
    inp, out, cache, cache_write, cost = _metrics_from_atif(data)
    if cost is not None and cost > 0:
        return _loop_draft(
            model=model,
            route=route,
            key_hash=key_hash,
            input_tokens=inp,
            output_tokens=out,
            cache_read_tokens=cache,
            cache_write_tokens=cache_write,
            cost_usd=cost,
            cost_source=COST_NATIVE,
        )
    estimated = estimate_cost_usd(model, inp, out, cache, cache_write)
    if estimated is not None:
        return _loop_draft(
            model=model,
            route=route,
            key_hash=key_hash,
            input_tokens=inp,
            output_tokens=out,
            cache_read_tokens=cache,
            cache_write_tokens=cache_write,
            cost_usd=estimated,
        )
    return _loop_draft(
        model=model,
        route=route,
        key_hash=key_hash,
        input_tokens=inp,
        output_tokens=out,
        cache_read_tokens=cache,
        cache_write_tokens=cache_write,
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
    if task_path is not None and not task_has_cua_signals(task_path):
        return []
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
    created_at: datetime | None = None,
) -> int:
    """Insert or no-op existing ``(trial_id, attempt, component)`` rows.

    Never updates an existing live row — failed attempts keep their spend;
    a later SUCCESS does not overwrite. Returns the number of rows inserted.

    ``created_at`` defaults to now (live settlement). Backfill must pass the
    trial's ``finished_at`` so admin windows bucket historical CUA spend with
    the period it actually occurred, not the sweep day.
    """
    if not drafts:
        return 0
    inserted = 0
    now = utcnow()
    stamped = created_at or now
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
            "created_at": stamped,
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
    created_at: datetime | None = None,
    session: AsyncSession | None = None,
) -> int:
    """Best-effort settlement write. Never raises into the trial path.

    When ``session`` is provided, uses it (caller owns commit). Otherwise opens
    a short write session. Pass ``created_at`` (usually ``trial.finished_at``)
    for historical backfill so admin windows do not dump old spend into today.
    """
    if job_dir is None:
        return 0
    try:
        drafts = build_verifier_cost_drafts(job_dir, task_path=task_path)
        if not drafts:
            return 0

        async def _write(s: AsyncSession) -> int:
            return await upsert_verifier_cost_rows(
                s,
                drafts=drafts,
                trial_id=trial_id,
                attempt=attempt,
                experiment_id=experiment_id,
                org_id=org_id,
                task_id=task_id,
                task_version_id=task_version_id,
                cost_source_override=cost_source_override,
                created_at=created_at,
            )

        if session is not None:
            return await _write(session)
        async with get_session() as s:
            return await _write(s)
    except Exception:
        log.exception(
            "verifier LLM cost settlement failed trial_id=%s attempt=%s",
            trial_id,
            attempt,
        )
        return 0


_BACKFILL_BATCH = 200

# Attempt-relative keys to try (Harbor CUA + SWE-M ux layout).
_BACKFILL_ARTIFACT_CANDIDATES = (
    ("verifier/ux/cua_judge_report.json", "cua_judge_report.json"),
    ("verifier/cua_judge_report.json", "cua_judge_report.json"),
    ("verifier/ux/trajectory.json", "trajectory.json"),
    ("verifier/trajectory.json", "trajectory.json"),
)


def _no_artifacts_sentinel() -> VerifierCostDraft:
    """Marks an attempt as inspected so cleanup does not rescan it forever."""
    return _loop_draft(
        model=None,
        route=ROUTE_OTHER,
        key_hash=None,
        cost_source=COST_BACKFILL,
        unpriced_reason=UNPRICED_NO_CUA_ARTIFACTS,
    )


async def backfill_verifier_costs_from_s3(*, limit: int = _BACKFILL_BATCH) -> int:
    """Best-effort historical CUA spend import. Cap ``limit`` trials per call.

    Selects finished agent trials with an S3 attempt prefix and no
    ``verifier_costs`` row for that attempt. Inserts priced rows when
    artifacts exist; otherwise inserts a null-cost sentinel so the same
    non-CUA trials are not reselected every sweep.
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
        existing = (
            select(VerifierCostModel.trial_id, VerifierCostModel.attempt)
            .where(VerifierCostModel.deleted_at.is_(None))
            .subquery()
        )
        # No load_only: this is a worker sweep, not a compact FE response path,
        # and a stray load_only trips the CI load_only_guard tripwire.
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
        failures = 0
        for trial in candidates:
            try:
                layout = await resolve_trial_artifact_layout(trial, storage)
                if (
                    layout.mode is TrialArtifactMode.UNAVAILABLE
                    or not layout.artifact_prefix
                ):
                    n = await upsert_verifier_cost_rows(
                        session,
                        drafts=[_no_artifacts_sentinel()],
                        trial_id=trial.id,
                        attempt=int(trial.attempts or 1),
                        experiment_id=trial.experiment_id,
                        org_id=trial.org_id,
                        task_id=trial.task_id,
                        task_version_id=trial.task_version_id,
                        created_at=trial.finished_at,
                    )
                    inserted_total += n
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
                        (verifier_dir / local_name).write_bytes(body)
                        found_any = True
                    if not found_any:
                        drafts = [_no_artifacts_sentinel()]
                        n = await upsert_verifier_cost_rows(
                            session,
                            drafts=drafts,
                            trial_id=trial.id,
                            attempt=int(trial.attempts or 1),
                            experiment_id=trial.experiment_id,
                            org_id=trial.org_id,
                            task_id=trial.task_id,
                            task_version_id=trial.task_version_id,
                            created_at=trial.finished_at,
                        )
                    else:
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
                            created_at=trial.finished_at,
                            session=session,
                        )
                    inserted_total += n
            except Exception:
                failures += 1
                if failures <= 3:
                    log.exception(
                        "verifier cost backfill failed trial_id=%s",
                        trial.id,
                    )
                continue
        if failures > 3:
            log.warning(
                "verifier cost backfill: %s additional trial failures suppressed",
                failures - 3,
            )
        return inserted_total
