"""Side-effectful probe overlay: stage related logs + rewrite instruction.md.

Shared by both runners — the local in-process runner (``local_runner``) and
the Modal/cloud worker (``workers/queue/trial_handler``) — so probe trials
behave identically in dev and production. The pure rendering/selection logic
lives in :mod:`oddish.worker.probe_overlay`; this module adds the DB + S3 +
filesystem effects.

The caller must pass a *writable* task dir (a temp copy, never a canonical or
mounted task path) since ``apply_probe_overlay`` mutates ``instruction.md`` in
place and writes the staged logs underneath it.
"""

from __future__ import annotations

import logging
import shutil
from pathlib import Path

from sqlalchemy import select

from oddish.core.skills import list_skills_core
from oddish.db import TrialModel, get_session, get_storage_client
from oddish.worker.probe_overlay import (
    AGENT_BRIEF_NAME,
    HARBOR_DIR_NAME,
    MAX_BYTES_PER_FILE,
    MAX_FILES_PER_TRIAL,
    MAX_RELATED_TRIALS,
    PROBE_SYSTEM_FRAMING,
    RELATED_CONTAINER_DIR,
    RELATED_DIR_NAME,
    render_probe_instruction,
    select_related_trials,
)
from oddish.worker.skills_overlay import SkillBundle, materialize_skills

logger = logging.getLogger(__name__)


async def stage_related_trial_logs(
    work_task_dir: Path, task_id: str, current_trial_id: str
) -> bool:
    """Download prior real (non-probe) attempts' logs into the work dir.

    Stages files into ``<work_task_dir>/related_trials/<trial_id>/`` (visible
    at :data:`RELATED_CONTAINER_DIR` once the runner uploads the work dir to the
    probe-harness root). The runner pulls the artifacts here, so no S3
    credentials enter the agent's container.
    Per-trial and per-file failures are logged and skipped; counts and sizes
    are capped. Returns True if anything was staged.
    """
    async with get_session() as session:
        result = await session.execute(
            select(TrialModel).where(TrialModel.task_id == task_id)
        )
        siblings = list(result.scalars().all())

    related = select_related_trials(siblings, current_trial_id=current_trial_id)
    if not related:
        return False

    storage = get_storage_client()
    dest_root = work_task_dir / RELATED_DIR_NAME
    staged_any = False

    for trial in related[:MAX_RELATED_TRIALS]:
        prefix = f"tasks/{task_id}/trials/{trial.id}/"
        try:
            keys = await storage.list_keys(prefix)
        except Exception:
            logger.exception(
                "probe: list_keys failed for %s; skipping", trial.id
            )
            continue
        staged_for_trial = 0
        for key in keys:
            if staged_for_trial >= MAX_FILES_PER_TRIAL:
                logger.warning(
                    "probe: capped related logs for %s at %d files",
                    trial.id,
                    MAX_FILES_PER_TRIAL,
                )
                break
            rel = key[len(prefix):]
            # Skip the prefix root and any hidden path segment.
            if not rel or any(
                part.startswith(".") for part in rel.split("/") if part
            ):
                continue
            try:
                content = await storage.download_bytes(key)
            except Exception:
                logger.exception(
                    "probe: download failed for %s; skipping", key
                )
                continue
            if len(content) > MAX_BYTES_PER_FILE:
                logger.info(
                    "probe: skipping large artifact %s (%d bytes)",
                    key,
                    len(content),
                )
                continue
            dest = dest_root / trial.id / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(content)
            staged_for_trial += 1
            staged_any = True

    return staged_any


def stage_harbor_source(work_task_dir: Path) -> bool:
    """Copy the *live* harbor package source into ``work_task_dir/harbor_src``.

    Resolves harbor from the running interpreter (``harbor.__file__``) so the
    staged source is byte-for-byte the code that actually builds the env and
    scores the trial -- otherwise a bug the agent "finds" in the source might
    not exist in the live harness, making the exploit theater. The runner
    uploads the work dir to the probe-harness root, so this lands at
    :data:`HARBOR_CONTAINER_DIR`. Failures are logged and skipped; they never
    block the probe. Returns True if staged.
    """
    try:
        import harbor

        src = Path(harbor.__file__).resolve().parent
    except Exception:
        logger.exception("probe: could not locate harbor source; skipping")
        return False

    dest = work_task_dir / HARBOR_DIR_NAME
    try:
        shutil.copytree(
            src,
            dest,
            ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "*.pyo", ".git"),
            dirs_exist_ok=True,
        )
    except Exception:
        logger.exception("probe: staging harbor source failed")
        return False
    return True


async def stage_org_skills(skills_root: Path, *, org_id: str | None) -> int:
    """Materialize the org's shared skills (+ global seeds) under
    ``skills_root/<name>/<relative_path>``.

    ``skills_root`` is meant to be passed to Harbor as an ``AgentConfig.skills``
    entry: Harbor's ``resolve_skills`` accepts a root whose every child dir holds
    a ``SKILL.md`` (exactly this layout), uploads each ``<name>/`` skill into the
    sandbox, and the claude-code agent registers them into Claude's config dir so
    the agent discovers them. Skills are written from Postgres at stage time, so
    they reach the (network-isolated) sandbox without the agent fetching anything.

    Best-effort and per-skill resilient: a DB failure stages nothing, and one
    malformed skill is skipped without dropping the others. Returns the number
    of skills actually staged; never raises.
    """
    try:
        async with get_session() as session:
            skills = await list_skills_core(session, org_id=org_id)
            bundles = [
                SkillBundle(
                    name=s.name,
                    files=[(f.relative_path, f.content) for f in s.files],
                )
                for s in skills
            ]
    except Exception:
        logger.exception("probe: loading org skills failed")
        return 0

    staged = 0
    for bundle in bundles:
        try:
            materialize_skills([bundle], skills_root)
            staged += 1
        except Exception:
            logger.exception("probe: staging skill %r failed", bundle.name)
    return staged


def collect_visibility(task_dir: Path) -> list[str]:
    """List the top-level entries staged under the probe-harness root.

    Everything staged for the probe (``tests/``, ``solution/``, ``environment/``,
    the staged ``related_trials/`` + ``harbor_src/``, ``task.toml``, ...) is
    uploaded under :data:`PROBE_HARNESS_DIR` — none of it reaches the real
    agent's ``/app``. This returns those top-level entries (sorted, dirs keep a
    trailing ``/``) so the visibility map can enumerate the harness contents.
    ``instruction.md`` is excluded (it is the probe's own prompt, delivered as a
    string); hidden entries are skipped. Call this AFTER staging so the staged
    dirs are included.
    """
    reserved = {"instruction.md"}
    probe_only: list[str] = []
    for child in sorted(task_dir.iterdir(), key=lambda p: p.name):
        if child.name in reserved or child.name.startswith("."):
            continue
        probe_only.append(f"{child.name}/" if child.is_dir() else child.name)

    return probe_only


async def apply_probe_overlay(
    task_dir: Path,
    *,
    task_id: str,
    trial_id: str,
    extra_instructions: str,
    time_budget_sec: float | None = None,
) -> None:
    """Stage related logs and rewrite ``task_dir/instruction.md`` in place.

    ``task_dir`` MUST be a writable temp copy of the task. Staging failures
    degrade gracefully (the related-logs section is softened); they never
    block the probe from running.

    (Org skill injection is handled separately by the runners via
    ``stage_org_skills`` + ``AgentConfig.skills`` -- NOT here, because skills
    must reach the sandbox through Harbor's skill-upload path, not the task dir
    which Harbor never mounts into the container.)
    """
    try:
        has_related = await stage_related_trial_logs(
            task_dir, task_id, trial_id
        )
    except Exception:
        logger.exception("probe: staging related trial logs failed")
        has_related = False

    # Stage harbor's own source as a reward-hack surface (read-only, in-mount,
    # network-immune). Best-effort: never blocks the probe.
    try:
        stage_harbor_source(task_dir)
    except Exception:
        logger.exception("probe: staging harbor source failed")

    instr_path = task_dir / "instruction.md"
    original = instr_path.read_text() if instr_path.exists() else ""

    # Save the real agent's brief verbatim so the probe can study it as the
    # *other* agent's instructions, and enumerate the harness contents now that
    # everything (related_trials/, harbor_src/) is staged.
    (task_dir / AGENT_BRIEF_NAME).write_text(original)
    probe_only = collect_visibility(task_dir)

    instr_path.write_text(
        render_probe_instruction(
            PROBE_SYSTEM_FRAMING,
            extra_instructions,
            original,
            related_dir=RELATED_CONTAINER_DIR,
            has_related=has_related,
            time_budget_sec=time_budget_sec,
            probe_only_paths=probe_only,
        )
    )
