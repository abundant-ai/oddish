"""DB-backed recorder for the ``oddish.core.llm`` funnel.

Wired via ``set_usage_recorder`` at server/worker startup; CLI processes leave
the recorder unset. Uses its own session so a failed write never poisons the
caller's transaction. ``LLMUsageRow`` field names deliberately mirror
``LLMUsageModel`` columns 1:1.
"""

from __future__ import annotations

from dataclasses import asdict

from oddish.core.llm import LLMUsageRow


async def record_llm_usage(row: LLMUsageRow) -> None:
    from oddish.db.connection import get_session
    from oddish.db.models import LLMUsageModel

    async with get_session() as session:
        session.add(LLMUsageModel(**asdict(row)))
