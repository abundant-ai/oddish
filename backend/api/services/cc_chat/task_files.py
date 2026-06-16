from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from oddish.db.models import TaskModel, TaskVersionModel, TrialModel
from oddish.db.storage import resolve_trial_s3_prefix


class TaskNotFound(Exception):
    """Raised when a task-scope chat references a task that doesn't exist
    in the caller's org."""

# Heavy/binary noise that wastes upload time and adds no chat value.
_SKIP_PARTS = frozenset({"node_modules", "__pycache__", "sessions", "backups", "skills"})


def _should_skip(rel: str) -> bool:
    return any(part in _SKIP_PARTS for part in rel.split("/"))


async def collect_task_version_files(
    session: AsyncSession,
    storage,
    *,
    task_name: str,
    org_id: str | None,
    max_total_bytes: int = 50_000_000,
) -> tuple[int | None, dict[int, list[str]], list[tuple[str, bytes]], bool, set[str]]:
    """Return (current_version, {version: [trial_id,...]}, files, truncated,
    probe_trial_ids).

    Resolves the task by ``name`` within the org (the per-task chat is keyed by
    the human task name, which is unique per org). `files` are
    (workspace_rel_path, bytes) tuples laid out as
    jobs/v{version}/{trial_id}/{rel}. Stops once the running byte total would
    exceed max_total_bytes (sets truncated=True). `probe_trial_ids` is the
    subset of collected trials with is_probe set, so the chat can flag them as
    distinct from regular runs. Raises TaskNotFound if the task doesn't exist."""
    task_q = select(TaskModel).where(TaskModel.name == task_name).options(
        selectinload(TaskModel.current_version)
    )
    if org_id is not None:
        task_q = task_q.where(TaskModel.org_id == org_id)
    task = (await session.execute(task_q)).scalars().first()
    if task is None:
        raise TaskNotFound(task_name)
    current_version = task.current_version.version if task.current_version else None

    version_rows = (
        await session.execute(
            select(TaskVersionModel)
            .where(TaskVersionModel.task_id == task.id)
            .order_by(TaskVersionModel.version.desc())
        )
    ).scalars().all()

    version_trials: dict[int, list[str]] = {}
    probe_trial_ids: set[str] = set()
    files: list[tuple[str, bytes]] = []
    total = 0
    truncated = False

    for v in version_rows:
        trial_q = select(TrialModel).where(
            TrialModel.task_id == task.id,
            TrialModel.task_version_id == v.id,
            TrialModel.superseded_by_trial_id.is_(None),
        )
        if org_id is not None:
            trial_q = trial_q.where(TrialModel.org_id == org_id)
        trials = (await session.execute(trial_q)).scalars().all()
        version_trials[v.version] = [t.id for t in trials]
        probe_trial_ids.update(t.id for t in trials if t.is_probe)

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
                files.append((f"jobs/v{v.version}/{t.id}/{rel}", data))
            if truncated:
                break
        if truncated:
            break

    return current_version, version_trials, files, truncated, probe_trial_ids
