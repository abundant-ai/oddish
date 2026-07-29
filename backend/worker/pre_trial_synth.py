"""Hosted pre-trial synthesis: audits a task's source (verifier/oracle/info-
leakage) via AnalyzerBlock + PreTrialBlock. The worker downloads the task
source from S3 and a worker-local claude-code run reads it with Read/Glob --
the same CLAUDE_CLI backend the post-trial classifier uses, with no sandbox and
no oddish-CLI install.

Registered into core's pre-trial hook (``register_pre_trial_synth``) at worker
container load. oddish/ can't import backend/, so this implementation is
injected the same way the sandbox LLM-client factory is. ``run_task_qa_job``
invokes the registered hook only when ``settings.pre_trial_enabled``; this
module then resolves the per-organization override (``pre_trial_analysis_enabled``
in org settings) and returns ``None`` when the org has opted out, which releases
the caller's version claim.
"""

from __future__ import annotations

import shutil

from sqlalchemy import select

from oddish.analyze.models import ActionItem
from oddish.blocks.analyzer.analyzer_block import (
    AnalyzerBlock,
    AnalyzerInput,
    AnalyzerType,
)
from oddish.blocks.analyzer.analyzer_llm_client import LLMClientType
from oddish.blocks.analyzer.claude_cli_client import CliConfig
from oddish.blocks.analyzer.pre_trial.pre_trial_block import PreTrialBlock
from oddish.config import settings
from oddish.core.prompts import resolve_prompt_core
from oddish.core.task_source import resolve_task_source_location
from oddish.db import PromptKind, get_session
from oddish.db.models import TaskModel
from oddish.db.storage import resolve_task_directory
from oddish.workers.queue.qa_handler import (
    PreTrialSynthResult,
    register_pre_trial_enabled_check,
    register_pre_trial_synth,
)
from models import OrganizationModel


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
) -> PreTrialSynthResult | None:
    """PreTrialSynthFn implementation backed by PreTrialBlock/AnalyzerBlock.

    Downloads the audited task version's source to a temp directory and runs the
    audit through an AnalyzerBlock on the worker-local CLAUDE_CLI backend (the
    same runner the post-trial classifier uses). ``task_version_id`` is the
    version being audited (claimed by the caller); file access is pinned to it,
    so the snapshot the auditor reads matches the version the findings are
    stored against. Never completes the task and never touches verdict state --
    that boundary lives in ``sync_pre_trial_to_task_version``, which the caller
    (``run_task_qa_job``) invokes with these items. The block's spend and id ride
    back on the result because they are knowable only here, where the block that
    ran is still in scope. Returns ``None`` (skip; the caller releases its claim)
    when the task's org has opted out of pre-trial analysis.
    """
    org_id, enabled = await _resolve_org_pre_trial(task_id)
    if not enabled:
        return None
    if org_id is None:
        raise RuntimeError(f"Cannot resolve org_id for task {task_id}")

    async with get_session() as session:
        # Pre-trial audits a task version, not a single trial or user, so only
        # org/task scope is meaningful here.
        prompt, ver = await resolve_prompt_core(
            session,
            PromptKind.QA_PRE_TRIAL.value,
            org_id=org_id,
            user_id=None,
            experiment_id=None,
            task_id=task_id,
            trial_id=None,
        )
        prompt_template = ver.content
        prompt_version = ver.version
        prompt_id = prompt.id

    block_obj = PreTrialBlock(
        task_id=task_id, trial_ids=trial_ids, prompt_template=prompt_template
    )

    task_s3_key, task_path = await resolve_task_source_location(
        task_id, task_version_id
    )
    task_dir, temp_task_dir, _ = await resolve_task_directory(
        task_id, task_s3_key=task_s3_key, task_path=task_path
    )
    try:
        block = AnalyzerBlock(
            analyzer_type=AnalyzerType.PRE_TRIAL,
            llm_client_type=LLMClientType.CLAUDE_CLI,
            input=AnalyzerInput(
                input={
                    "task_id": task_id,
                    "task_version_id": task_version_id,
                    "trial_ids": trial_ids,
                }
            ),
            task_id=task_id,
            prompt=block_obj.build_prompt(),
            model=settings.pre_trial_model,
            # The CLI yields a --output-format json envelope, not the model's
            # bare answer; this unwraps it before schema validation.
            output_transform=block_obj.to_action_items_from_cli,
            cli_config=CliConfig(
                cwd=task_dir,
                timeout=timeout or settings.pre_trial_timeout,
            ),
            block_metadata={
                "prompt_key": PromptKind.QA_PRE_TRIAL.value,
                "prompt_version": prompt_version,
                "prompt_id": prompt_id,
            },
        )
        # CliConfig.timeout (set above) is the sole deadline: it bounds the
        # claude subprocess from inside and kills it cleanly on expiry. An outer
        # asyncio.wait_for would race that inner timeout and, if it won, cancel
        # the run mid-communicate() -- orphaning the subprocess (the CLI
        # client's aclose is a no-op) and prematurely failing successful audits.
        # Mirrors the classifier's CLAUDE_CLI path.
        result = await block.run()
    finally:
        # The worker owns the downloaded temp dir now (no sandbox aclose to
        # delete it), so clean it up on every exit path or worker disk fills.
        if temp_task_dir is not None:
            shutil.rmtree(temp_task_dir, ignore_errors=True)

    data = result.output or {"items": []}
    return PreTrialSynthResult(
        items=[ActionItem(**it) for it in data.get("items", [])],
        cost_usd=block.usage.cost_usd if block.usage else None,
        block_id=block.id,
    )


async def _pre_trial_enabled(task_id: str) -> bool:
    """Org-level gate, checked by core before it claims a task version."""
    _, enabled = await _resolve_org_pre_trial(task_id)
    return enabled


# Importing this module (from backend.worker.functions) installs the hooks.
register_pre_trial_synth(synthesize_task_pre_trial)
register_pre_trial_enabled_check(_pre_trial_enabled)
