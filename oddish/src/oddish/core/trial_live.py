from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from oddish.db import TrialEventModel, TrialModel

LIVE_EVENTS_PAGE_LIMIT = 500


def build_live_response(trial: Any, events: list[Any], *, after_seq: int) -> dict:
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
        "next_seq": events[-1].seq if events else after_seq,
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


async def read_trial_live(
    session: AsyncSession,
    trial: TrialModel,
    *,
    attempt: int | None = None,
    after_seq: int = 0,
) -> dict:
    effective_after_seq = after_seq if attempt in (None, trial.attempts) else 0
    events = (
        (
            await session.execute(
                select(TrialEventModel)
                .where(
                    TrialEventModel.trial_id == trial.id,
                    TrialEventModel.attempt == trial.attempts,
                    TrialEventModel.seq > effective_after_seq,
                )
                .order_by(TrialEventModel.seq)
                .limit(LIVE_EVENTS_PAGE_LIMIT)
            )
        )
        .scalars()
        .all()
    )
    return build_live_response(trial, list(events), after_seq=effective_after_seq)
