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
    HARBOR_DIR_NAME,
    MAX_BYTES_PER_FILE,
    MAX_FILES_PER_TRIAL,
    MAX_RELATED_TRIALS,
    PROBE_SYSTEM_FRAMING,
    RELATED_CONTAINER_DIR,
    RELATED_DIR_NAME,
    TASK_SOURCE_DIR_NAME,
    render_probe_instruction,
    select_related_trials,
)
from oddish.worker.skills_overlay import SkillBundle, materialize_skills

logger = logging.getLogger(__name__)


async def stage_related_trial_logs(
    work_task_dir: Path,
    task_id: str,
    current_trial_id: str,
    *,
    target_trial_id: str | None = None,
) -> bool:
    """Download prior real (non-probe) attempts' logs into the build context.

    Stages files into ``<work_task_dir>/environment/related_trials/<trial_id>/``.
    Harbor builds the agent image from ``environment/`` as the Docker build
    context, so these reach the agent at ``/app/related_trials`` only once the
    Dockerfile COPYs them in (see ``_inject_build_context_copies``). The runner
    pulls the artifacts here, so no S3 credentials enter the agent's container.
    Per-trial and per-file failures are logged and skipped; counts and sizes
    are capped. Returns True if anything was staged.
    """
    async with get_session() as session:
        result = await session.execute(
            select(TrialModel).where(TrialModel.task_id == task_id)
        )
        siblings = list(result.scalars().all())

    related = select_related_trials(
        siblings, current_trial_id=current_trial_id, target_trial_id=target_trial_id
    )
    if not related:
        return False

    storage = get_storage_client()
    dest_root = work_task_dir / "environment" / RELATED_DIR_NAME
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


# excluded from task_source: environment/ is already baked into the image, and
# the staging subdirs themselves must never be copied into a copy of themselves.
_TASK_SOURCE_EXCLUDE = {
    "environment",
    TASK_SOURCE_DIR_NAME,
    RELATED_DIR_NAME,
    HARBOR_DIR_NAME,
}


def collect_task_source(work_task_dir: Path) -> int:
    """Copy the task dir (minus environment/ and staging subdirs) into
    ``<work_task_dir>/environment/task_source/``. Returns the number of files
    copied.

    The dest lives inside ``environment/`` (the Docker build context) so the
    Dockerfile can COPY it to ``/app/task_source`` -- ``environment`` is in
    ``_TASK_SOURCE_EXCLUDE``, so we never recurse into the build context we are
    writing into. Oversize files are skipped (mirrors the related-logs caps).
    Best-effort: per-file failures are logged and skipped.
    """
    dest_root = work_task_dir / "environment" / TASK_SOURCE_DIR_NAME
    copied = 0
    for entry in sorted(work_task_dir.iterdir()):
        if entry.name in _TASK_SOURCE_EXCLUDE or entry.name.startswith("."):
            continue
        for src in [entry] if entry.is_file() else entry.rglob("*"):
            if not src.is_file():
                continue
            try:
                if src.stat().st_size > MAX_BYTES_PER_FILE:
                    logger.info("probe: skipping large task file %s", src)
                    continue
                rel = src.relative_to(work_task_dir)
                dst = dest_root / rel
                dst.parent.mkdir(parents=True, exist_ok=True)
                dst.write_bytes(src.read_bytes())
                copied += 1
            except Exception:
                logger.exception("probe: copying task file %s failed", src)
    return copied


def _inject_build_context_copies(task_dir: Path) -> None:
    """Make staged build-context dirs reach the agent at /app/<name>.

    Harbor builds the agent image from ``task_dir/environment`` as the Docker
    build context, so files only reach ``/app`` if the Dockerfile COPYs them.
    We append idempotent COPY lines for each staged dir that exists. No-op when
    there is no Dockerfile (e.g. prebuilt-image tasks): the files simply are not
    delivered and the instruction's related-logs section degrades gracefully.
    """
    env_dir = task_dir / "environment"
    dockerfile = env_dir / "Dockerfile"
    if not dockerfile.exists():
        return

    # If a .dockerignore would exclude our dirs, the COPY fails the build.
    # Defensively un-ignore them.
    dockerignore = env_dir / ".dockerignore"
    if dockerignore.exists():
        di = dockerignore.read_text()
        negations = [
            f"!{name}"
            for name in (TASK_SOURCE_DIR_NAME, RELATED_DIR_NAME)
            if (env_dir / name).is_dir() and f"!{name}" not in di
        ]
        if negations:
            dockerignore.write_text(di.rstrip() + "\n" + "\n".join(negations) + "\n")

    existing = dockerfile.read_text()
    copies = []
    for name in (TASK_SOURCE_DIR_NAME, RELATED_DIR_NAME):
        if not (env_dir / name).is_dir():
            continue
        line = f"COPY {name} /app/{name}"
        if line not in existing:
            copies.append(line)
    if copies:
        dockerfile.write_text(
            existing.rstrip()
            + "\n\n# probe overlay: expose staged dirs to the agent at /app\n"
            + "\n".join(copies)
            + "\n"
        )


def stage_harbor_source(work_task_dir: Path) -> bool:
    """Copy the *live* harbor package source into ``work_task_dir/harbor_src``.

    Resolves harbor from the running interpreter (``harbor.__file__``) so the
    staged source is byte-for-byte the code that actually builds the env and
    scores the trial -- otherwise a bug the agent "finds" in the source might
    not exist in the live harness, making the exploit theater. Failures are
    logged and skipped; they never block the probe. Returns True if staged.

    NOTE: this stages to the work-dir root, not ``environment/``, so it does
    not currently reach the agent's ``/app`` (Harbor does not mount the task
    dir -- it builds the image from ``environment/`` as the Docker build
    context). Delivering harbor source is out of scope here.
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


async def apply_probe_overlay(
    task_dir: Path,
    *,
    task_id: str,
    trial_id: str,
    extra_instructions: str,
    probe_scope: str | None = None,
    target_trial_id: str | None = None,
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
    if probe_scope == "task":
        has_related = False  # task scope uses the S3 MCP server, not staging
    else:
        try:
            has_related = await stage_related_trial_logs(
                task_dir, task_id, trial_id, target_trial_id=target_trial_id
            )
        except Exception:
            logger.exception("probe: staging related trial logs failed")
            has_related = False

    # Every probe gets the whole task dir (minus environment/).
    try:
        collect_task_source(task_dir)
    except Exception:
        logger.exception("probe: collecting task source failed")

    # Wire the staged build-context dirs into the Dockerfile so they reach the
    # agent at /app. Best-effort: a failure here must never block the probe.
    try:
        _inject_build_context_copies(task_dir)
    except Exception:
        logger.exception("probe: injecting build-context COPY lines failed")

    # Stage harbor's own source as a reward-hack surface (read-only, in-mount,
    # network-immune). Best-effort: never blocks the probe.
    try:
        stage_harbor_source(task_dir)
    except Exception:
        logger.exception("probe: staging harbor source failed")

    instr_path = task_dir / "instruction.md"
    original = instr_path.read_text() if instr_path.exists() else ""

    instr_path.write_text(
        render_probe_instruction(
            PROBE_SYSTEM_FRAMING,
            extra_instructions,
            original,
            related_dir=RELATED_CONTAINER_DIR,
            has_related=has_related,
            time_budget_sec=time_budget_sec,
        )
    )
