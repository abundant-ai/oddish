from contextlib import asynccontextmanager

from oddish.db import TrialModel, get_session


@asynccontextmanager
async def _trial_session(trial_id: str, *, allow_missing: bool = False):
    async with get_session() as session:
        trial = await session.get(TrialModel, trial_id)
        if not trial and not allow_missing:
            raise RuntimeError(f"Trial {trial_id} not found in database")
        yield session, trial
        if trial:
            await session.commit()
