"""Hosted pre-trial synthesis: audits a task's source (verifier/oracle/info-
leakage) via AnalyzerBlock + PreTrialBlock, running the agent in a Daytona
sandbox that can ``oddish pull`` the task files.

Registered into core's pre-trial hook (``register_pre_trial_synth``) at worker
container load. oddish/ can't import backend/, so the sandbox-provisioning
implementation is injected here the same way the sandbox LLM-client factory is.
``run_task_qa_job`` invokes the registered hook only when
``settings.pre_trial_enabled``; unset, this module registers a function that is
simply never called.
"""

from __future__ import annotations

import asyncio

from sqlalchemy import select

from oddish.analyze.models import ActionItem
from oddish.blocks.analyzer.analyzer_block import (
    AnalyzerBlock,
    AnalyzerInput,
    AnalyzerType,
)
from oddish.blocks.analyzer.analyzer_llm_client import LLMClientType
from oddish.blocks.analyzer.pre_trial.pre_trial_block import PreTrialBlock
from oddish.config import api_base_url_for_modal_app, settings
from oddish.core.prompts import get_prompt_core
from oddish.db import get_session
from oddish.db.models import TaskModel
from oddish.workers.queue.qa_handler import register_pre_trial_synth
from worker.pre_trial_sandbox import provision_oddish_sandbox_client


async def _resolve_org_id(task_id: str) -> str | None:
    async with get_session() as session:
        return await session.scalar(
            select(TaskModel.org_id).where(TaskModel.id == task_id)
        )


async def synthesize_task_pre_trial(
    task_id: str, trial_ids: list[str], timeout: float
) -> list[ActionItem]:
    """PreTrialSynthFn implementation backed by PreTrialBlock/AnalyzerBlock.

    Self-provisions a sandbox client that can ``oddish pull`` the task's
    source, then runs the audit through an AnalyzerBlock (the same runner the
    verdict path uses). Never completes the task and never touches verdict
    state -- that boundary lives in ``sync_pre_trial_to_task``, which the
    caller (``run_task_qa_job``) invokes with these items.
    """
    async with get_session() as session:
        _, ver = await get_prompt_core(session, "pre_trial_qa")
        prompt_template = ver.content
        active_version = ver.version

    org_id = await _resolve_org_id(task_id)
    if org_id is None:
        # mint_internal_read_key's org_id is typed `str`, not `str | None` --
        # fail loudly here instead of letting a missing/deleted task's org
        # surface as a confusing type error (or a None-scoped key) downstream.
        raise RuntimeError(f"Cannot resolve org_id for task {task_id}")

    block_obj = PreTrialBlock(
        task_id=task_id, trial_ids=trial_ids, prompt_template=prompt_template
    )
    # Explicit override wins; otherwise derive from the Modal app identity so
    # prod and PR previews resolve automatically (mirrors api/app.py's cc_chat
    # orchestrator wiring).
    api_base_url = settings.public_api_base_url or api_base_url_for_modal_app()
    client = await provision_oddish_sandbox_client(
        org_id=org_id,
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
            block_metadata={
                "prompt_key": "pre_trial_qa",
                "prompt_version": active_version,
            },
        )
        result = await asyncio.wait_for(
            block.run(), timeout=timeout or settings.pre_trial_timeout
        )
    finally:
        await client.aclose()

    data = result.output or {"items": []}
    return [ActionItem(**it) for it in data.get("items", [])]


# Importing this module (from backend.worker.functions) installs the hook.
register_pre_trial_synth(synthesize_task_pre_trial)
