"""Hosted pre-trial synthesis: audits a task's source (verifier/oracle/info-
leakage) via AnalyzerBlock + PreTrialBlock, running the agent in a
Daytona sandbox that can ``oddish pull`` the task files.

Registered over the core QA handler (QaJobHandler.pre_trial_synth_fn) at
worker container load, gated on settings.pre_trial_via_analyzer_block --
following verdict_synth.py's shape: gate registration, not handler
internals, so unsetting the flag reverts pre-trial to the legacy no-op with
no branching.
"""

from __future__ import annotations

import asyncio

from sqlalchemy import select

from api.services.blocks.analyzer.analyzer_block import (
    AnalyzerBlock,
    AnalyzerInput,
    AnalyzerType,
)
from api.services.blocks.analyzer.analyzer_llm_client import LLMClientType
from api.services.blocks.analyzer.pre_trial.pre_trial_block import PreTrialBlock
from worker.pre_trial_sandbox import provision_oddish_sandbox_client
from oddish.analyze.models import ActionItem
from oddish.config import api_base_url_for_modal_app, settings
from oddish.core.prompts import get_active_prompt_content
from oddish.db import get_session
from oddish.db.models import TaskModel
from oddish.workers.jobs.handlers import QaJobHandler

_PRE_TRIAL_TIMEOUT = 180.0


async def _resolve_org_id(task_id: str) -> str | None:
    async with get_session() as session:
        return await session.scalar(
            select(TaskModel.org_id).where(TaskModel.id == task_id)
        )


async def pre_trial_block_synth(
    task_id: str, trial_ids: list[str], timeout: float
) -> list[ActionItem]:
    """PreTrialSynthFn implementation backed by PreTrialBlock/AnalyzerBlock.

    Self-provisions a sandbox client that can ``oddish pull`` the task's
    source, following the same gate/provisioning pattern as verdict_synth's
    AnalyzerBlock usage. Never completes the task and never touches verdict
    state -- that boundary lives in ``sync_pre_trial_to_task``, which this
    function's caller (``run_task_qa_job``) invokes.
    """
    async with get_session() as session:
        prompt_template = await get_active_prompt_content(session, "pre_trial_qa")

    block_obj = PreTrialBlock(
        task_id=task_id, trial_ids=trial_ids, prompt_template=prompt_template
    )
    # Explicit override wins; otherwise derive from the Modal app identity so
    # prod and PR previews resolve automatically (mirrors api/app.py's
    # cc_chat orchestrator wiring).
    api_base_url = settings.public_api_base_url or api_base_url_for_modal_app()
    client = await provision_oddish_sandbox_client(
        org_id=await _resolve_org_id(task_id),
        model=settings.pre_trial_model,
        api_key=None,
        api_base_url=api_base_url,
    )
    try:
        block = AnalyzerBlock(
            analyzer_type=AnalyzerType.PRE_TRIAL,
            llm_client_type=LLMClientType.SANDBOX,
            input=AnalyzerInput(input={"task_id": task_id, "trial_ids": trial_ids}),
            prompt=block_obj.build_prompt(),
            model=settings.pre_trial_model,
            output_transform=block_obj.to_action_items,
            client=client,
            block_metadata={"prompt_key": "pre_trial_qa"},
        )
        result = await asyncio.wait_for(
            block.run(), timeout=timeout or _PRE_TRIAL_TIMEOUT
        )
    finally:
        await client.aclose()

    data = result.output or {"items": []}
    return [ActionItem(**it) for it in data.get("items", [])]


class PreTrialBlockQaJobHandler(QaJobHandler):
    """Same QA orchestration as QaJobHandler -- only the pre-trial-synthesis
    strategy differs."""

    pre_trial_synth_fn = staticmethod(pre_trial_block_synth)


def install_pre_trial_block_qa_handler() -> bool:
    """Swap the core QA handler for the AnalyzerBlock-backed pre-trial one.
    Returns whether it was installed, so the caller can log the mode."""
    from oddish.workers.jobs import register

    if not settings.pre_trial_via_analyzer_block:
        return False
    register(PreTrialBlockQaJobHandler(), override=True)
    return True
