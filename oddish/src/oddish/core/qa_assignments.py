"""QA job assignments — pure core logic.

An assignment binds one registry prompt to a QA lifecycle ``stage`` at a scope,
so "run this check after every trial on this task" is data rather than a code
change. ``stage`` / ``llm_client_type`` / ``allow_oddish_cli`` are the terms
``run_custom_qa_core`` already uses: an assignment is recognisably "a saved
custom QA run that fires automatically".

Callers own the transaction; these functions never commit (they ``flush`` so
ids and defaults populate). Org filtering is passed in as a plain ``org_id``
like ``custom_qa`` does -- hosted-auth concepts (AuthContext, operator org)
stay in ``backend/``.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib

from sqlalchemy import and_, func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from oddish.blocks.analyzer.analyzer_llm_client import LLMClientType
from oddish.config import api_base_url_for_modal_app, settings
from oddish.db.models import (
    AnalyzerRunModel,
    JobStatus,
    PromptModel,
    QAAssignmentModel,
    QAStage,
    WorkerJobKind,
    utcnow,
)
from oddish.workers.jobs import EnqueueRequest, enqueue_worker_job


# ``qa_assignments.scope_type`` / ``scope_id`` are both NOT NULL (unlike
# ``prompts``, where global is NULL/NULL), so the installation-wide row is
# spelled ("global", ""). Every write normalizes through here; lookups match on
# scope_type alone, so the id half is never load-bearing.
GLOBAL_SCOPE_TYPE = "global"
GLOBAL_SCOPE_ID = ""

QA_ASSIGNMENT_SCOPE_TYPES = frozenset(
    {GLOBAL_SCOPE_TYPE, "org", "user", "experiment", "task", "trial"}
)


@dataclass(frozen=True)
class ResolvedAssignment:
    """One effective assignment plus where it was inherited from."""

    assignment: QAAssignmentModel
    prompt: PromptModel
    # The version that would run: the pinned ``prompt_version``, else the
    # registry's latest. ``None`` means the referenced prompt has no versions
    # at all -- a hard error at assign time, skipped-with-warning at run time.
    effective_version: int | None
    inherited_from: str

    @property
    def stage(self) -> str:
        return self.assignment.stage

    @property
    def kind(self) -> str:
        return self.prompt.kind


def scope_label(scope_type: str, scope_id: str | None) -> str:
    """Render a scope the way the CLI and API both report it."""
    if scope_type == GLOBAL_SCOPE_TYPE:
        return GLOBAL_SCOPE_TYPE
    return f"{scope_type}:{scope_id}" if scope_id else scope_type


def normalize_scope(scope_type: str, scope_id: str | None) -> tuple[str, str]:
    """Map a caller-supplied scope onto the NOT NULL column pair."""
    if scope_type not in QA_ASSIGNMENT_SCOPE_TYPES:
        raise ValueError(f"unsupported QA assignment scope: {scope_type}")
    if scope_type == GLOBAL_SCOPE_TYPE:
        return GLOBAL_SCOPE_TYPE, GLOBAL_SCOPE_ID
    if not scope_id:
        raise ValueError(f"{scope_type} scope requires a scope_id")
    return scope_type, scope_id


def _effective_version(assignment: QAAssignmentModel, versions) -> int | None:
    if assignment.prompt_version is not None:
        return assignment.prompt_version
    return max((v.version for v in versions), default=None)


def _org_visible(org_id: str | None):
    """A row is visible if it is the installation-wide default or this org's."""
    return or_(QAAssignmentModel.org_id.is_(None), _org_equals(org_id))


def _org_equals(org_id: str | None):
    """``column == None`` renders as ``= NULL``, which is never true, so the
    installation-wide (org_id NULL) rows need an explicit IS NULL."""
    if org_id is None:
        return QAAssignmentModel.org_id.is_(None)
    return QAAssignmentModel.org_id == org_id


def _scope_matches(scope_type: str, scope_id: str | None):
    if scope_type == GLOBAL_SCOPE_TYPE:
        return QAAssignmentModel.scope_type == GLOBAL_SCOPE_TYPE
    return and_(
        QAAssignmentModel.scope_type == scope_type,
        QAAssignmentModel.scope_id == scope_id,
    )


async def _select_assignments(session: AsyncSession, where) -> list[tuple]:
    """Assignments joined to their prompt. The join is required, not an
    optimization: the override key is the *referenced prompt's kind*."""
    result = await session.execute(
        select(QAAssignmentModel, PromptModel)
        .join(PromptModel, PromptModel.id == QAAssignmentModel.prompt_id)
        .where(where)
    )
    return list(result.all())


async def resolve_qa_assignments_core(
    session: AsyncSession,
    *,
    stage: str | None = None,
    org_id: str | None,
    user_id: str | None,
    experiment_id: str | None,
    task_id: str | None,
    trial_id: str | None,
) -> list[ResolvedAssignment]:
    """The effective set of QA jobs for a subject, unioned across scopes.

    Walks **broadest to narrowest** -- global, org, user, task, experiment,
    trial -- accumulating into a dict. Unlike ``resolve_prompt_core`` (strict
    narrowest-wins, one winner) assignments *union*: adding one extra check to
    a task must not silently disable the org's default post-trial QA.

    - key absent  -> add (a new job at this scope)
    - key present -> overwrite (narrower config wins)
    - ``enabled=false`` -> drop the key (suppress a broader default)

    The key is ``(stage, prompts.kind)``, reached through the ``prompt_id``
    join -- *not* ``(stage, prompt_id)``. An org default and a task override
    point at different prompt rows (the org's ``QA_POST_TRIAL`` and the task's),
    so keying on ``prompt_id`` would make them distinct and run **both**.

    Task outranks experiment, matching ``resolve_prompt_core``. The two are not
    nested, so the order is a decision rather than a containment fact: a task
    override encodes durable knowledge about that task (a brittle verifier, a
    known failure mode), while an experiment override encodes intent for one
    run. Letting the broader scope win would suppress the task's knowledge
    exactly where attention is highest.
    """
    candidates: list[tuple[str, str | None]] = [
        (GLOBAL_SCOPE_TYPE, None),
        ("org", org_id),
        ("user", user_id),
        ("experiment", experiment_id),
        ("task", task_id),
        ("trial", trial_id),
    ]
    active = [
        (scope_type, scope_id)
        for scope_type, scope_id in candidates
        if scope_type == GLOBAL_SCOPE_TYPE or scope_id
    ]

    where = and_(
        _org_visible(org_id),
        or_(*[_scope_matches(st, sid) for st, sid in active]),
    )
    if stage is not None:
        where = and_(where, QAAssignmentModel.stage == stage)

    rows = await _select_assignments(session, where)

    rank = {scope_type: index for index, (scope_type, _) in enumerate(active)}
    # Ties within a scope (no DB uniqueness on (scope, stage, kind)) settle on
    # the most recently updated row, so a repeat write always wins.
    rows.sort(key=lambda row: (rank[row[0].scope_type], row[0].updated_at))

    resolved: dict[tuple[str, str], ResolvedAssignment] = {}
    for assignment, prompt in rows:
        key = (assignment.stage, prompt.kind)
        if not assignment.enabled:
            resolved.pop(key, None)
            continue
        versions = await prompt.awaitable_attrs.versions
        resolved[key] = ResolvedAssignment(
            assignment=assignment,
            prompt=prompt,
            effective_version=_effective_version(assignment, versions),
            inherited_from=scope_label(assignment.scope_type, assignment.scope_id),
        )
    return sorted(resolved.values(), key=lambda r: (r.stage, r.kind))


async def list_qa_assignments_core(
    session: AsyncSession,
    *,
    scope_type: str,
    scope_id: str | None,
    org_id: str | None,
    stage: str | None = None,
) -> list[ResolvedAssignment]:
    """Rows defined at *exactly* this scope -- what the edit UI needs.

    Disabled rows are included: a suppression row is a thing you must be able
    to see in order to remove it.
    """
    scope_type, scope_id = normalize_scope(scope_type, scope_id)
    where = and_(_org_visible(org_id), _scope_matches(scope_type, scope_id))
    if stage is not None:
        where = and_(where, QAAssignmentModel.stage == stage)

    out = []
    for assignment, prompt in await _select_assignments(session, where):
        versions = await prompt.awaitable_attrs.versions
        out.append(
            ResolvedAssignment(
                assignment=assignment,
                prompt=prompt,
                effective_version=_effective_version(assignment, versions),
                inherited_from=scope_label(scope_type, scope_id),
            )
        )
    return sorted(out, key=lambda r: (r.stage, r.kind))


async def get_qa_assignment_core(
    session: AsyncSession, *, assignment_id: str
) -> QAAssignmentModel | None:
    return await session.get(QAAssignmentModel, assignment_id)


async def upsert_qa_assignment_core(
    session: AsyncSession,
    *,
    prompt: PromptModel,
    stage: str,
    scope_type: str,
    scope_id: str | None,
    org_id: str | None,
    model: str,
    llm_client_type: str,
    reasoning_effort: str | None = None,
    allow_oddish_cli: bool = False,
    prompt_version: int | None = None,
    enabled: bool = True,
    created_by_user_id: str | None = None,
    overwrite_runner_config: bool = True,
) -> QAAssignmentModel:
    """Create, or update in place, the assignment for this scope/stage/kind.

    Matched on the referenced prompt's **kind**, not ``prompt_id``, for the
    same reason resolution is: two rows at one scope naming the same kind
    through different prompt rows would make the effective job ambiguous.
    Re-assigning therefore repoints the existing row rather than adding a
    competing one.

    ``overwrite_runner_config=False`` flips ``enabled`` on an existing row
    without touching its model/backend/effort/CLI/version. A bare
    ``qa-jobs disable`` carries no runner config, so it must not clobber a
    configured assignment's settings back to stage defaults on the way to
    suppressing it. On a fresh row the passed values still seed the NOT NULL
    columns regardless.
    """
    if stage not in {s.value for s in QAStage}:
        raise ValueError(f"unsupported QA stage: {stage}")
    scope_type, scope_id = normalize_scope(scope_type, scope_id)
    if allow_oddish_cli and llm_client_type != "Sandbox":
        raise ValueError("Oddish CLI access is only available to the sandbox backend")

    existing = None
    for assignment, candidate_prompt in await _select_assignments(
        session,
        and_(
            _org_equals(org_id),
            _scope_matches(scope_type, scope_id),
            QAAssignmentModel.stage == stage,
        ),
    ):
        if candidate_prompt.kind == prompt.kind:
            existing = assignment
            break

    creating = existing is None
    if creating:
        existing = QAAssignmentModel(
            org_id=org_id, scope_type=scope_type, scope_id=scope_id, stage=stage
        )
        session.add(existing)

    existing.prompt_id = prompt.id
    existing.enabled = enabled
    if creating or overwrite_runner_config:
        existing.prompt_version = prompt_version
        existing.model = model
        existing.reasoning_effort = reasoning_effort
        existing.llm_client_type = llm_client_type
        existing.allow_oddish_cli = allow_oddish_cli
        existing.created_by_user_id = created_by_user_id
    await session.flush()
    return existing


async def delete_qa_assignment_core(
    session: AsyncSession, *, assignment: QAAssignmentModel
) -> None:
    """Soft delete; the session-level filter hides the row from every read."""
    assignment.deleted_at = utcnow()
    await session.flush()


async def qa_assignment_status_core(
    session: AsyncSession, *, assignment_ids: list[str], org_id: str | None
) -> dict[str, dict[str, int]]:
    """Run counts per assignment, as ``{assignment_id: {status: count}}``.

    Aggregated from ``analyzer_runs``, not ``analyzer_blocks``: the post-trial
    path writes ``trials.analysis`` directly and emits no block row, so a
    block-keyed rollup would report scoped post-trial jobs as never used.
    """
    if not assignment_ids:
        return {}
    rows = (
        await session.execute(
            select(
                AnalyzerRunModel.qa_assignment_id,
                AnalyzerRunModel.status,
                func.count().label("count"),
            )
            .where(
                AnalyzerRunModel.qa_assignment_id.in_(assignment_ids),
                AnalyzerRunModel.org_id.is_(None)
                if org_id is None
                else AnalyzerRunModel.org_id == org_id,
            )
            .group_by(AnalyzerRunModel.qa_assignment_id, AnalyzerRunModel.status)
        )
    ).all()
    out: dict[str, dict[str, int]] = {}
    for assignment_id, status, count in rows:
        value = status.value if hasattr(status, "value") else str(status)
        out.setdefault(assignment_id, {})[value] = count
    return out


async def enqueue_qa_assignment_runs_core(
    session: AsyncSession,
    *,
    stage: str,
    stage_event_key: str,
    org_id: str | None,
    user_id: str | None,
    experiment_id: str | None,
    task_id: str | None,
    trial_id: str | None,
    task_version_id: str | None = None,
    run_scope_type: str,
    run_scope_id: str,
) -> list[AnalyzerRunModel]:
    """Resolve and enqueue every assignment for one lifecycle event.

    The caller owns the transaction. Existing ``(assignment, event)`` runs are
    returned as no-ops, making retries of trial finalization and sweep
    submission safe. The database's partial unique index is the final
    concurrency guard.
    """
    if stage not in {item.value for item in QAStage}:
        raise ValueError(f"unsupported QA stage: {stage}")
    if not stage_event_key:
        raise ValueError("stage_event_key is required")

    resolved = await resolve_qa_assignments_core(
        session,
        stage=stage,
        org_id=org_id,
        user_id=user_id,
        experiment_id=experiment_id,
        task_id=task_id,
        trial_id=trial_id,
    )
    created: list[AnalyzerRunModel] = []
    for item in resolved:
        assignment = item.assignment
        existing = await session.scalar(
            select(AnalyzerRunModel).where(
                AnalyzerRunModel.qa_assignment_id == assignment.id,
                AnalyzerRunModel.stage_event_key == stage_event_key,
            )
        )
        if existing is not None:
            continue

        versions = await item.prompt.awaitable_attrs.versions
        version = next(
            (
                candidate
                for candidate in versions
                if candidate.version == item.effective_version
            ),
            None,
        )
        # Assignment writes reject version-less prompts and invalid pins. Keep
        # lifecycle hooks resilient if historical data was edited manually.
        if version is None:
            continue

        backend = (
            "sandbox"
            if assignment.llm_client_type == LLMClientType.SANDBOX.value
            else "api"
        )
        digest = hashlib.sha256(version.content.encode()).hexdigest()
        system_prompt = (
            "You are running an automatic Oddish QA check. "
            f"The lifecycle stage is {stage}; the exact subject is "
            f"{run_scope_type} {run_scope_id}. Treat the assigned prompt as "
            "the QA hypothesis and report concrete evidence."
        )
        oddish_api_base_url = None
        if assignment.allow_oddish_cli:
            system_prompt += (
                " The oddish CLI is installed and authenticated in this "
                "ephemeral sandbox. Use it when useful and report every "
                "command and created resource."
            )
            oddish_api_base_url = (
                settings.public_api_base_url or api_base_url_for_modal_app()
            ).rstrip("/")

        config = {
            "scope": {"type": run_scope_type, "id": run_scope_id},
            # The subject the lifecycle block attributes cost and lineage to:
            # a post-trial run charges its trial (via analyzer_id) and carries
            # task_id for lineage; a pre-trial run charges its task.
            "task_id": task_id,
            "trial_id": trial_id,
            # Pins pre-trial file access to the exact version this event is for,
            # so a re-upload between enqueue and execution can't swap the source.
            "task_version_id": task_version_id,
            "stage": stage,
            "stage_event_key": stage_event_key,
            "qa_assignment_id": assignment.id,
            "assignment_scope": {
                "type": assignment.scope_type,
                "id": assignment.scope_id,
            },
            "inherited_from": item.inherited_from,
            "prompt_id": item.prompt.id,
            "prompt_key": item.prompt.kind,
            "prompt_version": version.version,
            "prompt": {
                "id": item.prompt.id,
                "kind": item.prompt.kind,
                "version": version.version,
                "version_id": version.id,
                "sha256": digest,
            },
            "model": assignment.model,
            "reasoning_effort": assignment.reasoning_effort,
            "backend": backend,
            "oddish_cli_enabled": bool(assignment.allow_oddish_cli),
            "oddish_api_base_url": oddish_api_base_url,
            "system_prompt": system_prompt,
            "automatic": True,
        }
        run = AnalyzerRunModel(
            org_id=org_id,
            prompt_version_id=version.id,
            triggered_by_user_id=user_id,
            scope_type=run_scope_type,
            scope_id=run_scope_id,
            model=assignment.model,
            reasoning_effort=assignment.reasoning_effort,
            llm_client_type=assignment.llm_client_type,
            status=JobStatus.QUEUED,
            run_config=config,
            qa_assignment_id=assignment.id,
            stage_event_key=stage_event_key,
        )
        # The pre-loop SELECT skips runs already committed, but a concurrent
        # enqueue for the same event can commit between that read and this
        # insert. Give each run its own savepoint so the partial unique index
        # -- the documented final guard -- rejects that one duplicate as an
        # idempotent no-op instead of aborting the batch and rolling back the
        # sibling runs already inserted for other assignments.
        try:
            async with session.begin_nested():
                session.add(run)
                await session.flush()
                await enqueue_worker_job(
                    session,
                    EnqueueRequest(
                        kind=WorkerJobKind.ANALYZER_BLOCK,
                        queue_key=settings.normalize_queue_key(assignment.model),
                        payload={"analyzer_run_id": run.id},
                        subject_table="analyzer_runs",
                        subject_id=run.id,
                        org_id=org_id,
                    ),
                )
        except IntegrityError:
            continue
        created.append(run)
    return created
