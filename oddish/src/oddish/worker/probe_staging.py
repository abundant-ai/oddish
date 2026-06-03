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
from pathlib import Path

from sqlalchemy import select

from oddish.db import TrialModel, get_session, get_storage_client
from oddish.worker.probe_overlay import (
    MAX_BYTES_PER_FILE,
    MAX_FILES_PER_TRIAL,
    MAX_RELATED_TRIALS,
    PROBE_SYSTEM_FRAMING,
    RELATED_CONTAINER_DIR,
    RELATED_DIR_NAME,
    render_probe_instruction,
    select_related_trials,
)

logger = logging.getLogger(__name__)


async def stage_related_trial_logs(
    work_task_dir: Path, task_id: str, current_trial_id: str
) -> bool:
    """Download prior real (non-probe) attempts' logs into the work dir.

    Stages files into ``<work_task_dir>/related_trials/<trial_id>/`` (visible
    at ``/app/related_trials`` once Harbor mounts the task). The runner pulls
    the artifacts here, so no S3 credentials enter the agent's container.
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


async def apply_probe_overlay(
    task_dir: Path,
    *,
    task_id: str,
    trial_id: str,
    extra_instructions: str,
) -> None:
    """Stage related logs and rewrite ``task_dir/instruction.md`` in place.

    ``task_dir`` MUST be a writable temp copy of the task. Staging failures
    degrade gracefully (the related-logs section is softened); they never
    block the probe from running.
    """
    try:
        has_related = await stage_related_trial_logs(
            task_dir, task_id, trial_id
        )
    except Exception:
        logger.exception("probe: staging related trial logs failed")
        has_related = False

    instr_path = task_dir / "instruction.md"
    original = instr_path.read_text() if instr_path.exists() else ""
    instr_path.write_text(
        render_probe_instruction(
            PROBE_SYSTEM_FRAMING,
            extra_instructions,
            original,
            related_dir=RELATED_CONTAINER_DIR,
            has_related=has_related,
        )
    )
