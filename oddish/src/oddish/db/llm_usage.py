"""DB-backed recorder for the ``oddish.core.llm`` funnel.

Wired via ``set_usage_recorder`` at server/worker startup; CLI processes leave
the recorder unset. Uses its own session so a failed write never poisons the
caller's transaction.
"""

from __future__ import annotations

from oddish.core.llm import LLMUsageRow


async def record_llm_usage(row: LLMUsageRow) -> None:
    from oddish.db.connection import get_session
    from oddish.db.models import LLMUsageModel

    async with get_session() as session:
        session.add(
            LLMUsageModel(
                handler=row.handler,
                model=row.model,
                input_tokens=row.input_tokens,
                output_tokens=row.output_tokens,
                cache_read_tokens=row.cache_read_tokens,
                cache_write_tokens=row.cache_write_tokens,
                cost_usd=row.cost_usd,
                cost_basis=row.cost_basis,
                trial_id=row.trial_id,
                task_id=row.task_id,
                experiment_id=row.experiment_id,
                duration_ms=row.duration_ms,
                success=row.success,
                error=row.error,
            )
        )
