"""CUA / LLM verifier spend: locate artifacts, price drafts, persist rows.

Distinct from ``trials.cost_usd`` (solver) and ``analysis_costs`` (QA).

Covers Harbor ``type = "cua"`` (``verifier/``) and SWE-Marathon inline CUA
(``verifier/ux/``, ``cua_judge_report.json``).

Route uses the model id spelling: ``anthropic/…`` → Claude console;
``bedrock/…`` and Bedrock inference-profile ids → AWS.
"""

from __future__ import annotations

import json
import logging
import math
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from sqlalchemy import func, or_, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from oddish.core.harbor_artifacts import cache_write_tokens_from_trajectory
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
    """Return input, output, cache_read, cache_write, cost from an ATIF object.

    Cache writes use the shared Harbor reader: sum ATIF steps first, then
    ``final_metrics.extra``. Computer-1 often omits ``total_cost_usd`` and
    only records ``cache_creation_input_tokens`` per step.
    """
    final_metrics = data.get("final_metrics")
    input_tokens = output_tokens = cache_tokens = cost = None
    if isinstance(final_metrics, dict):
        input_tokens = _as_int(final_metrics.get("total_prompt_tokens"))
        output_tokens = _as_int(final_metrics.get("total_completion_tokens"))
        cache_tokens = _as_int(final_metrics.get("total_cached_tokens"))
        cost = _as_float(final_metrics.get("total_cost_usd"))
    cache_write = cache_write_tokens_from_trajectory(data)
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
    if loop_model is None and traj_path is not None:
        traj = _load_json(traj_path)
        if isinstance(traj, dict):
            agent = traj.get("agent")
            if isinstance(agent, dict):
                loop_model = (
                    str(agent.get("model_name") or "").strip() or None
                )
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
    # Prefer the dedicated [verifier.cua] table when present.
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
    if out["model"] is None and out["judge_model"] is None:
        # Harbor multi-stage: [[verifiers]] with type = "cua".
        for block in re.split(r"\[\[verifiers\]\]", text, flags=re.IGNORECASE)[1:]:
            if not re.search(r'type\s*=\s*["\']cua["\']', block, re.IGNORECASE):
                continue
            bm = re.search(
                r"(?:^|\n)\s*model\s*=\s*[\"']([^\"']+)[\"']",
                block,
                re.IGNORECASE,
            )
            bj = re.search(
                r"(?:^|\n)\s*judge_model\s*=\s*[\"']([^\"']+)[\"']",
                block,
                re.IGNORECASE,
            )
            if bm:
                out["model"] = bm.group(1).strip()
            if bj:
                out["judge_model"] = bj.group(1).strip()
            if out["model"] or out["judge_model"]:
                break
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


def _judge_criteria_count(report: dict[str, Any]) -> int:
    """How many rubric rows the judge priced — list or map shaped reports."""
    verdicts = report.get("verdicts")
    if isinstance(verdicts, list):
        return max(len(verdicts), 1)
    if isinstance(verdicts, dict) and verdicts:
        return len(verdicts)
    for key in ("criteria", "cua_criteria", "results"):
        block = report.get(key)
        if isinstance(block, list) and block:
            return len(block)
        if isinstance(block, dict) and block:
            return len(block)
    return 1


def draft_judge_cost(bundle: CuaArtifactBundle) -> VerifierCostDraft | None:
    report = bundle.judge_report
    if not isinstance(report, dict):
        return None
    model = bundle.judge_model or bundle.loop_model
    route = infer_verifier_route(model)
    key_hash = _key_hash_for_route(route)
    criteria_count = _judge_criteria_count(report)
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
    """Return loop (+ judge) drafts when CUA artifacts are present.

    A missing or unreadable ``task_path`` is not a negative signal: live
    settlement may pass a downloaded copy the worker already deleted. Only
    skip when we can inspect the bundle and it is clearly not CUA.
    """
    if (
        task_path is not None
        and task_path.exists()
        and not task_has_cua_signals(task_path)
    ):
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

    Never updates a priced live row — failed attempts keep their spend; a
    later SUCCESS does not overwrite. A prior ``no_cua_artifacts`` sentinel
    (wrong-path backfill) IS replaced when a later pass finds real drafts.
    Returns the number of rows inserted or replaced.

    ``created_at`` defaults to now (live settlement). Backfill must pass the
    trial's ``finished_at`` so admin windows bucket historical CUA spend with
    the period it actually occurred, not the sweep day. Sentinel replacement
    restamps ``created_at`` to that same value; priced live rows stay
    untouched because the conflict ``WHERE`` only matches
    ``no_cua_artifacts``.
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
        update_cols = {
            key: values[key]
            for key in (
                "model",
                "route",
                "llm_key_hash",
                "input_tokens",
                "output_tokens",
                "cache_read_tokens",
                "cache_write_tokens",
                "cost_usd",
                "cost_source",
                "unpriced_reason",
                "created_at",
                "updated_at",
                "experiment_id",
                "org_id",
                "task_id",
                "task_version_id",
            )
        }
        stmt = (
            pg_insert(VerifierCostModel)
            .values(**values)
            .on_conflict_do_update(
                index_elements=["trial_id", "attempt", "component"],
                index_where=VerifierCostModel.deleted_at.is_(None),
                set_=update_cols,
                where=(
                    VerifierCostModel.unpriced_reason
                    == UNPRICED_NO_CUA_ARTIFACTS
                ),
            )
        )
        result = await session.execute(stmt)
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

_ATTEMPT_PREFIX = re.compile(r"^(?P<root>.*/)attempt-(?P<n>[1-9]\d*)$")


def _no_artifacts_sentinel() -> VerifierCostDraft:
    """Placeholder so a missed download can be replaced later."""
    return VerifierCostDraft(
        component=COMPONENT_LOOP,
        model=None,
        route=ROUTE_OTHER,
        llm_key_hash=None,
        input_tokens=None,
        output_tokens=None,
        cache_read_tokens=None,
        cache_write_tokens=None,
        cost_usd=None,
        cost_source=COST_BACKFILL,
        unpriced_reason=UNPRICED_NO_CUA_ARTIFACTS,
    )


def attempt_s3_prefix(trial_s3_key: str | None, attempt: int) -> str | None:
    """Sibling ``attempt-N/`` prefix derived from the stored attempt pointer."""
    if not trial_s3_key or attempt < 1:
        return None
    key = trial_s3_key.rstrip("/")
    match = _ATTEMPT_PREFIX.match(key)
    if match is None:
        return None
    return f"{match.group('root')}attempt-{attempt}/"


def _trial_result_has_cua_signal(result: Any) -> bool:
    """True when stored trial.result looks like a CUA/UX verifier ran."""
    if not isinstance(result, dict):
        return False
    return any(
        key in result
        for key in (
            "cua_criteria",
            "cua_rubric_score",
            "cua_passed_count",
            "cua_failed_count",
            "cua_passed_ids",
            "cua_partial_count",
        )
    )


def trial_needs_verifier_backfill(
    *,
    has_cua_result_signal: bool,
    covered_attempts: int,
    attempts: int,
) -> bool:
    """Whether a finished agent trial belongs in a backfill batch.

    Non-CUA trials are excluded so recent ordinary agent runs cannot fill
    the 200-trial cap. Any row — priced or ``no_cua_artifacts`` sentinel —
    counts as coverage so a correct-path miss cannot pin the newest-first
    batch. Sentinel replacement still happens when a later selected trial
    or live write finds real drafts.
    """
    if not has_cua_result_signal:
        return False
    max_attempt = max(int(attempts or 1), 1)
    return covered_attempts < max_attempt


async def _backfill_one_trial(session: AsyncSession, trial: Any, storage: Any) -> int:
    """Price missing attempts for one trial. Caller owns the session."""
    import tempfile

    from oddish.core.trial_artifacts import (
        TrialArtifactMode,
        resolve_trial_artifact_layout,
    )
    from oddish.db.storage import is_missing_object

    try:
        from botocore.exceptions import ClientError
    except ImportError:  # pragma: no cover
        ClientError = Exception  # type: ignore[misc, assignment]

    reopen_sentinels = _trial_result_has_cua_signal(trial.result)
    row_meta = (
        await session.execute(
            select(
                VerifierCostModel.attempt,
                VerifierCostModel.unpriced_reason,
            ).where(
                VerifierCostModel.trial_id == trial.id,
                VerifierCostModel.deleted_at.is_(None),
            )
        )
    ).all()
    have_real: set[int] = set()
    have_any: set[int] = set()
    for attempt_num, reason in row_meta:
        n = int(attempt_num)
        have_any.add(n)
        if reason != UNPRICED_NO_CUA_ARTIFACTS:
            have_real.add(n)
    max_attempt = max(int(trial.attempts or 1), 1)
    inserted_total = 0
    for attempt in range(1, max_attempt + 1):
        if attempt in have_real:
            continue
        if attempt in have_any and not reopen_sentinels:
            continue
        sibling = attempt_s3_prefix(trial.trial_s3_key, attempt)
        if sibling is None and attempt != max_attempt:
            n = await upsert_verifier_cost_rows(
                session,
                drafts=[_no_artifacts_sentinel()],
                trial_id=trial.id,
                attempt=attempt,
                experiment_id=trial.experiment_id,
                org_id=trial.org_id,
                task_id=trial.task_id,
                task_version_id=trial.task_version_id,
                created_at=trial.finished_at,
            )
            inserted_total += n
            have_any.add(attempt)
            continue
        pointer = SimpleNamespace(
            id=trial.id,
            trial_s3_key=sibling or trial.trial_s3_key,
            kind=getattr(trial, "kind", "agent") or "agent",
            attempts=attempt if sibling else (trial.attempts or 1),
            finished_at=trial.finished_at,
        )
        layout = await resolve_trial_artifact_layout(pointer, storage)
        if layout.mode is TrialArtifactMode.UNAVAILABLE or not layout.artifact_prefix:
            n = await upsert_verifier_cost_rows(
                session,
                drafts=[_no_artifacts_sentinel()],
                trial_id=trial.id,
                attempt=attempt,
                experiment_id=trial.experiment_id,
                org_id=trial.org_id,
                task_id=trial.task_id,
                task_version_id=trial.task_version_id,
                created_at=trial.finished_at,
            )
            inserted_total += n
            have_any.add(attempt)
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
                n = await upsert_verifier_cost_rows(
                    session,
                    drafts=[_no_artifacts_sentinel()],
                    trial_id=trial.id,
                    attempt=attempt,
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
                    attempt=attempt,
                    experiment_id=trial.experiment_id,
                    org_id=trial.org_id,
                    task_id=trial.task_id,
                    task_version_id=trial.task_version_id,
                    cost_source_override=COST_BACKFILL,
                    created_at=trial.finished_at,
                    session=session,
                )
            inserted_total += n
            have_any.add(attempt)
            if found_any:
                have_real.add(attempt)
    return inserted_total


async def backfill_verifier_costs_from_s3(*, limit: int = _BACKFILL_BATCH) -> int:
    """Best-effort historical CUA spend import. Cap ``limit`` trials per call.

    Only CUA-signaled finished agent trials with at least one attempt that
    has no ``verifier_costs`` row are selected, so recent ordinary agent
    runs and already-sentinel-covered misses cannot starve the batch. Each
    trial uses its own write session so one IntegrityError cannot roll back
    the sweep. Artifacts are read from the Harbor trial subdirectory, not
    the bare ``attempt-N/`` root.
    """
    from oddish.db import TrialModel, TrialStatus
    from oddish.db.storage import StorageClient

    async with get_session() as session:
        all_covered = (
            select(
                VerifierCostModel.trial_id.label("trial_id"),
                func.count(func.distinct(VerifierCostModel.attempt)).label(
                    "covered"
                ),
            )
            .where(VerifierCostModel.deleted_at.is_(None))
            .group_by(VerifierCostModel.trial_id)
            .subquery()
        )
        cua_signal = or_(
            TrialModel.result.has_key("cua_criteria"),
            TrialModel.result.has_key("cua_rubric_score"),
            TrialModel.result.has_key("cua_passed_count"),
            TrialModel.result.has_key("cua_failed_count"),
            TrialModel.result.has_key("cua_passed_ids"),
            TrialModel.result.has_key("cua_partial_count"),
        )
        # No load_only: this is a worker sweep, not a compact FE response path,
        # and a stray load_only trips the CI load_only_guard tripwire.
        candidates = (
            await session.execute(
                select(TrialModel)
                .outerjoin(all_covered, all_covered.c.trial_id == TrialModel.id)
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
                    cua_signal,
                    or_(
                        all_covered.c.trial_id.is_(None),
                        all_covered.c.covered < TrialModel.attempts,
                    ),
                )
                .order_by(TrialModel.finished_at.desc())
                .limit(limit)
            )
        ).scalars().all()
        trial_ids = [trial.id for trial in candidates]

    if not trial_ids:
        return 0

    storage = StorageClient()
    inserted_total = 0
    failures = 0
    for trial_id in trial_ids:
        try:
            async with get_session() as session:
                trial = await session.get(TrialModel, trial_id)
                if trial is None:
                    continue
                inserted_total += await _backfill_one_trial(session, trial, storage)
        except Exception:
            failures += 1
            if failures <= 3:
                log.exception(
                    "verifier cost backfill failed trial_id=%s",
                    trial_id,
                )
            continue
    if failures > 3:
        log.warning(
            "verifier cost backfill: %s additional trial failures suppressed",
            failures - 3,
        )
    return inserted_total
