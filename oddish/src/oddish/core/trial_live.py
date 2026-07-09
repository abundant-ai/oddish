from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from oddish.core.endpoints._common import get_trial_for_org_core
from oddish.db import TrialEventModel, TrialModel

LIVE_EVENTS_PAGE_LIMIT = 500


async def read_trial_live_for_id(
    session: AsyncSession, *, trial_id: str, org_id: str | None = None, **cursor
) -> dict:
    trial = await get_trial_for_org_core(session, trial_id=trial_id, org_id=org_id)
    return await read_trial_live(session, trial, **cursor)


async def read_trial_live(
    session: AsyncSession,
    trial: TrialModel,
    *,
    attempt: int | None = None,
    after_seq: int = 0,
) -> dict:
    effective_after_seq = after_seq if attempt in (None, trial.attempts) else 0
    events = (
        await session.scalars(
            select(TrialEventModel)
            .where(
                TrialEventModel.trial_id == trial.id,
                TrialEventModel.attempt == trial.attempts,
                TrialEventModel.seq > effective_after_seq,
            )
            .order_by(TrialEventModel.seq)
            .limit(LIVE_EVENTS_PAGE_LIMIT)
        )
    ).all()
    return {
        "attempt": trial.attempts,
        "events": [
            {
                "seq": event.seq,
                "kind": event.kind,
                "payload": event.payload,
                "created_at": event.created_at,
            }
            for event in events
        ],
        "next_seq": events[-1].seq if events else effective_after_seq,
        "usage": {
            "input_tokens": trial.input_tokens,
            "cache_tokens": trial.cache_tokens,
            "cache_write_tokens": trial.cache_write_tokens,
            "output_tokens": trial.output_tokens,
            "cost_usd": trial.cost_usd,
        },
        "harbor_stage": trial.harbor_stage,
        "done": trial.finished_at is not None,
    }
