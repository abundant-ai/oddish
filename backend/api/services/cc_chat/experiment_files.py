from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from oddish.db.models import TrialModel
from oddish.db.storage import resolve_trial_s3_prefix

from api.services.cc_chat.task_files import _should_skip


async def collect_experiment_files(
    session: AsyncSession,
    storage,
    *,
    experiment_id: str,
    org_id: str | None,
    max_total_bytes: int = 50_000_000,
) -> tuple[list[str], list[tuple[str, bytes]], bool, set[str]]:
    """Return (trial_ids, files, truncated, probe_trial_ids) for an experiment.

    Walks every non-superseded trial belonging to ``experiment_id`` (keyed by
    the direct ``TrialModel.experiment_id`` FK), resolves each trial's S3 prefix
    and downloads its artifacts. `files` are (workspace_rel_path, bytes) tuples
    laid out as ``jobs/{experiment_id}/{trial_id}/{rel}`` to match the experiment
    CLAUDE.md template. Stops once the running byte total would exceed
    max_total_bytes (sets truncated=True). `probe_trial_ids` is the subset of
    collected trials with is_probe set, so the chat can flag them distinctly. An
    unknown/empty experiment yields empty results rather than raising."""
    trial_q = select(TrialModel).where(
        TrialModel.experiment_id == experiment_id,
        TrialModel.superseded_by_trial_id.is_(None),
    )
    if org_id is not None:
        trial_q = trial_q.where(TrialModel.org_id == org_id)
    trial_q = trial_q.order_by(TrialModel.id)
    trials = (await session.execute(trial_q)).scalars().all()

    trial_ids = [t.id for t in trials]
    probe_trial_ids = {t.id for t in trials if t.is_probe}
    files: list[tuple[str, bytes]] = []
    total = 0
    truncated = False

    for t in trials:
        prefix = resolve_trial_s3_prefix(
            t.id, trial_s3_key=t.trial_s3_key, trial_result_path=t.harbor_result_path
        )
        for key in await storage.list_keys(prefix):
            rel = key[len(prefix):]
            if not rel or _should_skip(rel):
                continue
            if total > max_total_bytes:
                truncated = True
                break
            data = await storage.download_bytes(key)
            if total + len(data) > max_total_bytes:
                truncated = True
                break
            total += len(data)
            files.append((f"jobs/{experiment_id}/{t.id}/{rel}", data))
        if truncated:
            break

    return trial_ids, files, truncated, probe_trial_ids
