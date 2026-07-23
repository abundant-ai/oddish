"""Hosted pre-trial synthesis: audits a task's source (verifier/oracle/info-
leakage) via AnalyzerBlock + PreTrialBlock, running the agent in a Daytona
sandbox that can ``oddish pull`` the task files.

Registered into core's pre-trial hook (``register_pre_trial_synth``) at worker
container load. oddish/ can't import backend/, so the sandbox-provisioning
implementation is injected here the same way the sandbox LLM-client factory is.
``run_task_qa_job`` invokes the registered hook only when
``settings.pre_trial_enabled``; this module then resolves the per-organization
override (``pre_trial_analysis_enabled`` in org settings) and returns ``None``
when the org has opted out, which releases the caller's version claim.
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
from oddish.db import PromptKind, get_session
from oddish.db.models import TaskModel
from oddish.workers.queue.qa_handler import (
    PRE_TRIAL_LEASE_MARGIN_SECONDS,
    register_pre_trial_synth,
)
from models import OrganizationModel
from worker.pre_trial_sandbox import provision_oddish_sandbox_client

# Provisioning (sandbox create + claude-code/harbor/oddish-CLI installs) runs
# before the block-run wait_for, and the claim lease is sized as
# pre_trial_timeout + PRE_TRIAL_LEASE_MARGIN_SECONDS -- so provisioning must
# be bounded by the margin or a hang lets wall time outrun the lease. A
# timed-out provision may leak a half-created sandbox; Daytona's
# auto_delete_minutes reaps it.
_PROVISION_TIMEOUT_SECONDS = PRE_TRIAL_LEASE_MARGIN_SECONDS


_PRE_TRIAL_ANALYSIS_SETTING = "pre_trial_analysis_enabled"


async def _resolve_org_pre_trial(task_id: str) -> tuple[str | None, bool]:
    """The task's org_id plus that org's pre-trial opt-in. An explicit boolean
    in org settings wins; otherwise ``settings.pre_trial_enabled`` (already
    true when we're called -- the caller gates on it) is the default."""
    async with get_session() as session:
        row = (
            await session.execute(
                select(TaskModel.org_id, OrganizationModel.settings)
                .outerjoin(OrganizationModel, OrganizationModel.id == TaskModel.org_id)
                .where(TaskModel.id == task_id)
            )
        ).first()
        if row is None:
            return None, False
        org_settings = row.settings or {}
        enabled = org_settings.get(_PRE_TRIAL_ANALYSIS_SETTING)
        return row.org_id, (
            enabled if isinstance(enabled, bool) else settings.pre_trial_enabled
        )


async def synthesize_task_pre_trial(
    task_id: str, task_version_id: str, trial_ids: list[str], timeout: float
) -> list[ActionItem] | None:
    """PreTrialSynthFn implementation backed by PreTrialBlock/AnalyzerBlock.

    Self-provisions a sandbox client that can ``oddish pull`` the task's
    source, then runs the audit through an AnalyzerBlock (the same runner the
    verdict path uses). ``task_version_id`` is the version being audited (the
    task's current version, claimed by the caller); the sandbox pulls the
    task's *current* source, so the two match unless a new upload lands
    mid-audit -- the caller's store gate (``_pre_trial_store_allowed``)
    detects that and discards the result rather than persisting findings
    against the wrong snapshot. Never completes the task and never touches
    verdict state -- that boundary lives in ``sync_pre_trial_to_task_version``,
    which the caller (``run_task_qa_job``) invokes with these items. Returns
    ``None`` (skip; the caller releases its claim) when the task's org has
    opted out of pre-trial analysis.
    """
    org_id, enabled = await _resolve_org_pre_trial(task_id)
    if not enabled:
        return None
    if org_id is None:
        # mint_internal_read_key's org_id is typed `str`, not `str | None` --
        # fail loudly here instead of letting a missing/deleted task's org
        # surface as a confusing type error (or a None-scoped key) downstream.
        raise RuntimeError(f"Cannot resolve org_id for task {task_id}")

    async with get_session() as session:
        _, ver = await get_prompt_core(session, PromptKind.QA_PRE_TRIAL.value)
        prompt_template = ver.content
        prompt_version = ver.version

    block_obj = PreTrialBlock(
        task_id=task_id, trial_ids=trial_ids, prompt_template=prompt_template
    )
    # Explicit override wins; otherwise derive from the Modal app identity so
    # prod and PR previews resolve automatically (mirrors api/app.py's cc_chat
    # orchestrator wiring).
    api_base_url = settings.public_api_base_url or api_base_url_for_modal_app()
    client = await asyncio.wait_for(
        provision_oddish_sandbox_client(
            org_id=org_id,
            model=settings.pre_trial_model,
            api_key=None,
            api_base_url=api_base_url,
        ),
        timeout=_PROVISION_TIMEOUT_SECONDS,
    )
    try:
        block = AnalyzerBlock(
            analyzer_type=AnalyzerType.PRE_TRIAL,
            llm_client_type=LLMClientType.SANDBOX,
            input=AnalyzerInput(
                input={
                    "task_id": task_id,
                    "task_version_id": task_version_id,
                    "trial_ids": trial_ids,
                }
            ),
            prompt=block_obj.build_prompt(),
            model=settings.pre_trial_model,
            output_transform=block_obj.to_action_items,
            client=client,
            block_metadata={
                "prompt_key": PromptKind.QA_PRE_TRIAL.value,
                "prompt_version": prompt_version,
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
