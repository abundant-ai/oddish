from contextlib import asynccontextmanager

from oddish.db import TrialModel, get_session


@asynccontextmanager
async def _trial_session(
    trial_id: str,
    *,
    allow_missing: bool = False,
    with_for_update: bool = False,
):
    async with get_session() as session:
        # include_deleted: a soft-deleted trial must stay visible to the worker
        # so its late real outcome still settles (callers guard deleted_at).
        trial = await session.get(
            TrialModel,
            trial_id,
            with_for_update=with_for_update,
            execution_options={"include_deleted": True},
        )
        if not trial and not allow_missing:
            raise RuntimeError(f"Trial {trial_id} not found in database")
        yield session, trial
