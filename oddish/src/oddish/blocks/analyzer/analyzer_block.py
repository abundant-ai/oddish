from __future__ import annotations

import asyncio
import enum
import json
import logging
from collections.abc import MutableMapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable

from sqlalchemy import select

from oddish.analyze.analysis_cost import AnalysisUsage, build_analysis_cost_row
from oddish.db import generate_id, get_session
from oddish.db.models import (
    AnalyzerBlockModel,
    AnalyzerModel,
    JobStatus,
    TaskModel,
    TrialModel,
    utcnow,
)
from oddish.db.storage import get_storage_client

from oddish.blocks.analyzer.analyzer_llm_client import (
    AnalyzerLLMClient,
    LLMClientType,
    SandboxConfig,
    create_llm_client,
)
from oddish.blocks.analyzer.claude_cli_client import CliConfig
from oddish.blocks.block import Block


class AnalyzerType(str, enum.Enum):
    TRAJECTORY_FAILURE_ANALYSIS = "trajectory_failure_analysis"
    HEADROOM_ANALYSIS = "headroom_analysis"
    SCALING_ANALYSIS = "scaling_analysis"
    TRAJECTORY_SUMMARY = "trajectory_summary"
    TASK_VERDICT = "task_verdict"
    PRE_TRIAL = "pre_trial"
    POST_TRIAL = "post_trial"
    CUSTOM_QA = "custom_qa"


# The backend each analyzer needs, keyed by what the analyzer is permitted to
# do rather than by what happens to be importable in this process. Both
# POST_TRIAL and PRE_TRIAL read directories the worker already downloaded
# through Read/Glob and execute nothing, so a worker-local subprocess is
# sufficient -- pre-trial audits the task source the worker fetches for it,
# rather than pulling it itself inside a sandbox.
# Anything unlisted has no filesystem to bind to and talks to the provider API.
# Ceiling on the network round-trip that tears a client down (for SANDBOX, a
# Daytona delete). Generous, but bounded -- see the close in ``run``.
_CLIENT_CLOSE_TIMEOUT = 60.0


_REQUIRED_SUBSTRATE: dict[AnalyzerType, LLMClientType] = {
    AnalyzerType.POST_TRIAL: LLMClientType.CLAUDE_CLI,
    AnalyzerType.PRE_TRIAL: LLMClientType.CLAUDE_CLI,
}


def resolve_substrate(
    analyzer_type: AnalyzerType,
    *,
    sandbox_available: bool,
    force_sandbox: bool = False,
) -> LLMClientType:
    """Pick the execution backend for an analyzer.

    ``force_sandbox`` is the operator opt-in that lifts a normally worker-local
    analyzer into isolation; it is a deliberate setting, never inferred from
    whether the hosted sandbox client was imported. A sandbox that is required
    but unregistered raises instead of falling back, because the two backends
    stream different shapes and are read by different output transforms -- a
    silent downgrade hands the caller output its parser cannot decode.
    """
    required = _REQUIRED_SUBSTRATE.get(analyzer_type, LLMClientType.API)
    wants_sandbox = force_sandbox or required is LLMClientType.SANDBOX
    if wants_sandbox and not sandbox_available:
        raise RuntimeError(
            f"{analyzer_type.value} needs the hosted sandbox backend, which is "
            "not registered in this process"
        )
    return LLMClientType.SANDBOX if wants_sandbox else required


@dataclass
class AnalyzerInput:
    input: Any
    files_to_download: list[str] | None = None


@dataclass
class AnalyzerOutput:
    output: Any
    files_written: list[str] | None = None


def block_key_prefix(analyzer_type: AnalyzerType) -> str:
    """S3 prefix / log tag for a block, keyed by its analyzer type."""
    return f"analyzer/{analyzer_type.value}"


class _PrefixAdapter(logging.LoggerAdapter):
    def process(
        self, msg: str, kwargs: MutableMapping[str, Any]
    ) -> tuple[str, MutableMapping[str, Any]]:
        return f"[{self.extra['prefix']}] {msg}", kwargs


def block_logger(key_prefix: str) -> logging.LoggerAdapter:
    """A logger whose every record (including exceptions) is tagged with the
    block's key_prefix, so all of one block's output is greppable by type."""
    return _PrefixAdapter(
        logging.getLogger("oddish.analyzer_block"), {"prefix": key_prefix}
    )


def _block_row_kwargs(*, block_metadata: dict | None, **base) -> dict:
    """Pure kwargs builder for AnalyzerBlockModel, so prompt_key/prompt_version/
    prompt_id extraction from block_metadata is unit-testable without a DB."""
    md = block_metadata or {}
    base["prompt_key"] = md.get("prompt_key")
    base["prompt_version"] = md.get("prompt_version")
    base["prompt_id"] = md.get("prompt_id")
    base["block_metadata"] = block_metadata
    return base


class AnalyzerBlock(Block):
    """One composable analyzer job. Runs a prompt through a swappable backend,
    streams the output, and persists to S3 + DB on every exit path."""

    def __init__(
        self,
        *,
        analyzer_type: AnalyzerType,
        llm_client_type: LLMClientType,
        input: AnalyzerInput,
        prompt: str,
        analyzer_id: str | None = None,
        task_id: str | None = None,
        block_metadata: dict | None = None,
        system_prompt: str | None = None,
        model: str | None = None,
        max_tokens: int | None = None,
        response_format: Any | None = None,
        output_schema: dict | None = None,
        output_transform: Callable[[str], Any] | None = None,
        api_key: str | None = None,
        triggered_by_user_id: str | None = None,
        sandbox_config: SandboxConfig | None = None,
        client_creation_timeout: float | None = None,
        attribution_org_id: str | None = None,
        subject_type: str | None = None,
        subject_id: str | None = None,
        cli_config: CliConfig | None = None,
        client_close_timeout: float | None = _CLIENT_CLOSE_TIMEOUT,
        on_chunk: Callable[[str], None] | None = None,
    ) -> None:
        self.id = generate_id()
        self.analyzer_type = analyzer_type
        self.llm_client_type = llm_client_type
        self.input = input
        self.prompt = prompt
        self.analyzer_id = analyzer_id
        self.task_id = task_id
        # Who caused this spend, when that differs from who owns the subject.
        # Worker-triggered trajectory summaries leave this unset and cost
        # attribution falls back to the trial's billed user.
        self.triggered_by_user_id = triggered_by_user_id
        if input.files_to_download and llm_client_type == LLMClientType.API:
            raise ValueError(
                "files_to_download is sandbox-only; the API backend has no filesystem"
            )
        self._downloaded_files: dict[str, bytes] = {}
        self.system_prompt = system_prompt
        self.model = model
        if model:
            block_metadata = {**(block_metadata or {}), "model": model}
        self.block_metadata = block_metadata
        self._output_transform = output_transform
        # Only used when self-provisioning (client is None). api_key: None -> the
        # analyzer key / provider default. response_format is OpenAI-only and
        # output_schema is Anthropic-only -- the same structured-output intent
        # expressed per provider, so a caller that may run on either passes
        # both. The other backends ignore both.
        self._api_key = api_key
        self._max_tokens = max_tokens
        self._response_format = response_format
        self._output_schema = output_schema
        self._sandbox_config = sandbox_config
        self._client_creation_timeout = client_creation_timeout
        self._active_client: AnalyzerLLMClient | None = None
        self.attribution_org_id = attribution_org_id
        # What this block is ABOUT, as opposed to ``analyzer_id``, which is an
        # overloaded association id. Set explicitly by callers that know their
        # subject (see ``analyzer_block_handler``); cohort blocks leave it None
        # because they span many trials and have no single subject to charge.
        self.subject_type = subject_type
        self.subject_id = subject_id
        self._cli_config = cli_config
        self._client_close_timeout = client_close_timeout
        # Called with each streamed chunk as it arrives, so callers can show
        # live progress (for example the trial analysis log). Failures in the
        # hook are logged and never fail the block.
        self._on_chunk = on_chunk

        self.key_prefix = block_key_prefix(analyzer_type)
        self.log = block_logger(self.key_prefix)

        self.status: JobStatus = JobStatus.PENDING
        self.output: AnalyzerOutput | None = None
        self.error: str | None = None
        self.job_started_at: datetime | None = None
        self.job_ended_at: datetime | None = None
        self.job_duration_seconds: float | None = None
        self._chunks: list[str] = []
        self.usage: AnalysisUsage | None = None

    @property
    def s3_key(self) -> str:
        return f"{self.key_prefix}/{self.id}"

    async def save_to_s3(self, raw: bytes) -> None:
        """Upload the raw streamed bytes. Never raises -- an S3 failure must not
        block the DB write in _persist."""
        try:
            await get_storage_client().upload_bytes(
                raw, self.s3_key, content_type="application/x-ndjson"
            )
            self.log.info("saved %dB to s3 key=%s", len(raw), self.s3_key)
        except Exception:
            self.log.exception("save_to_s3 failed for key=%s", self.s3_key)

    async def save_to_db(self) -> None:
        """Insert the block row. Never raises -- persistence is best-effort on
        the failure path, and the caller has already logged the primary error."""
        try:
            async with get_session() as session:
                session.add(
                    AnalyzerBlockModel(
                        **_block_row_kwargs(
                            block_metadata=self.block_metadata,
                            id=self.id,
                            analyzer_id=self.analyzer_id,
                            task_id=self.task_id,
                            type=self.analyzer_type.value,
                            key_prefix=self.key_prefix,
                            llm_client_type=self.llm_client_type.value,
                            prompt=self.prompt,
                            input=self.input.input,
                            output=self.output.output if self.output else None,
                            status=self.status,
                            error=self.error,
                            job_started_at=self.job_started_at,
                            job_ended_at=self.job_ended_at,
                            job_duration_seconds=self.job_duration_seconds,
                        )
                    )
                )
            self.log.info("saved block row id=%s status=%s", self.id, self.status.value)
        except Exception:
            self.log.exception("save_to_db failed for id=%s", self.id)

    async def _cost_attribution(self, session) -> dict[str, str | None]:
        """Resolve the subject columns copied onto the immutable cost row.

        Resolution order:

        1. An explicit ``subject_type``/``subject_id`` (trial, task, or
           experiment). This is what callers that know their subject pass.
        2. The legacy paths: an explicit ``task_id``, or ``analyzer_id`` used
           as an overloaded association id (a trial id, else an AnalyzerModel
           id).

        ``attribution_org_id`` OVERRIDES the resolved org. It must not
        short-circuit resolution -- doing so is what left every custom-QA cost
        row scope-less, since the generic runner always passes one.

        Cohort blocks span many trials and resolve to a NULL subject
        deliberately: there is no single trial or experiment to charge.
        """
        blank: dict[str, str | None] = {
            "trial_id": None,
            "org_id": None,
            "experiment_id": None,
            "billed_user_id": self.triggered_by_user_id,
            "task_id": self.task_id,
            "analyzer_id": None,
        }

        resolved = await self._resolve_subject(session, blank)

        # Applied last so an explicit org wins over the subject's own, without
        # costing us the subject.
        if self.attribution_org_id is not None:
            resolved["org_id"] = self.attribution_org_id
        return resolved

    async def _resolve_subject(
        self, session, blank: dict[str, str | None]
    ) -> dict[str, str | None]:
        """Subject resolution for ``_cost_attribution``, org override aside."""
        if self.subject_type == "trial" and self.subject_id:
            return await self._attribute_to_trial(session, blank, self.subject_id)
        if self.subject_type == "task" and self.subject_id:
            return await self._attribute_to_task(session, blank, self.subject_id)
        if self.subject_type == "experiment" and self.subject_id:
            return {**blank, "experiment_id": self.subject_id}

        # Post-trial and trajectory-summary blocks may carry task_id for
        # lineage, but their spend belongs to the individual trial referenced
        # by analyzer_id. Skip the task path and resolve that trial below.
        trial_scoped_types = {
            AnalyzerType.POST_TRIAL,
            AnalyzerType.TRAJECTORY_SUMMARY,
        }
        if self.task_id and self.analyzer_type not in trial_scoped_types:
            return await self._attribute_to_task(session, blank, self.task_id)
        if not self.analyzer_id:
            return blank

        # ``analyzer_id`` is an overloaded association id: a trial id on the
        # trial path, an AnalyzerModel id on the cohort path.
        by_trial = await self._attribute_to_trial(session, blank, self.analyzer_id)
        if by_trial["trial_id"] is not None:
            return by_trial

        analyzer = (
            await session.execute(
                select(AnalyzerModel.org_id, AnalyzerModel.owner_user_id).where(
                    AnalyzerModel.id == self.analyzer_id
                )
            )
        ).first()
        if analyzer is None:
            return blank
        return {
            **blank,
            "org_id": analyzer.org_id,
            "billed_user_id": self.triggered_by_user_id or analyzer.owner_user_id,
            "analyzer_id": self.analyzer_id,
        }

    async def _attribute_to_trial(
        self, session, blank: dict[str, str | None], trial_id: str
    ) -> dict[str, str | None]:
        """Charge a single trial. Unknown id degrades to ``blank`` rather than
        raising -- accounting must never take down a block that produced
        output."""
        trial = (
            await session.execute(
                select(
                    TrialModel.id,
                    TrialModel.org_id,
                    TrialModel.experiment_id,
                    TrialModel.billed_user_id,
                ).where(TrialModel.id == trial_id)
            )
        ).first()
        if trial is None:
            return blank
        return {
            **blank,
            "trial_id": trial.id,
            "org_id": trial.org_id,
            "experiment_id": trial.experiment_id,
            # Viewer-triggered background work carries that viewer through its
            # queued payload; they caused the call even though a worker runs it
            # later. Internally-triggered work falls back to the trial owner.
            "billed_user_id": self.triggered_by_user_id or trial.billed_user_id,
        }

    async def _attribute_to_task(
        self, session, blank: dict[str, str | None], task_id: str
    ) -> dict[str, str | None]:
        task = (
            await session.execute(
                select(TaskModel.org_id, TaskModel.created_by_user_id).where(
                    TaskModel.id == task_id
                )
            )
        ).first()
        if task is None:
            return {**blank, "task_id": task_id}
        return {
            **blank,
            "task_id": task_id,
            "org_id": task.org_id,
            "billed_user_id": self.triggered_by_user_id or task.created_by_user_id,
        }

    async def record_cost(self) -> None:
        """Append this block's LLM spend to ``analysis_costs``.

        ``job_kind`` is the block's own ``analyzer_type`` -- the kind decides
        what the row is labelled, so every current and future analyzer kind is
        accounted for by construction instead of at each call site. Recorded on
        failure too: a block that errored still burned the tokens.

        Never raises -- accounting must not take down a block that already
        produced its output. The outcome is stamped onto the block instead, so
        lost spend is countable in Postgres rather than only in a log line.
        """
        if self.usage is None:
            self._stamp_cost_status("no_usage")
            return
        try:
            async with get_session() as session:
                session.add(
                    build_analysis_cost_row(
                        job_kind=self.analyzer_type.value,
                        usage=self.usage,
                        **await self._cost_attribution(session),
                    )
                )
            self._stamp_cost_status("recorded")
            self.log.info(
                "recorded cost job_kind=%s cost_usd=%s source=%s",
                self.analyzer_type.value,
                self.usage.cost_usd,
                self.usage.source,
            )
        except Exception:
            self._stamp_cost_status("failed")
            self.log.exception("record_cost failed for id=%s", self.id)

    def _stamp_cost_status(self, status: str) -> None:
        """Record on the block row whether this block's spend reached
        ``analysis_costs``: ``recorded`` | ``no_usage`` | ``failed``.

        The invariant this exists to make queryable: every successful block
        should be ``recorded``. Anything else on a block that burned tokens is
        spend that is missing from the ledger.
        """
        self.block_metadata = {**(self.block_metadata or {}), "cost_status": status}

    async def _create_client(self) -> AnalyzerLLMClient:
        create = create_llm_client(
            self.llm_client_type,
            model=self.model,
            api_key=self._api_key,
            max_tokens=self._max_tokens,
            response_format=self._response_format,
            output_schema=self._output_schema,
            sandbox_config=self._sandbox_config,
            cli_config=self._cli_config,
        )
        if self._client_creation_timeout is None:
            return await create
        return await asyncio.wait_for(create, timeout=self._client_creation_timeout)

    async def stream_output(self):
        """Yield output through the client owned by this run."""
        client = self._active_client
        if client is None:
            raise RuntimeError("AnalyzerBlock client is unavailable outside run()")
        async for chunk in client.stream(self.prompt, system_prompt=self.system_prompt):
            self._chunks.append(chunk)
            self.log.debug("chunk %d (len=%d)", len(self._chunks), len(chunk))
            if self._on_chunk is not None:
                try:
                    self._on_chunk(chunk)
                except Exception:  # noqa: BLE001
                    self.log.exception("on_chunk hook failed")
            yield chunk

    async def _download_requested_files(self) -> dict[str, str]:
        """Pull each requested path off the sandbox. A file the agent never
        wrote decodes to "" so one missing batch costs only its own batch
        instead of failing the whole cohort."""
        files: dict[str, str] = {}
        for path in self.input.files_to_download or []:
            try:
                download_file = getattr(self._active_client, "_download_file")
                raw = await download_file(path)
            except Exception:
                self.log.warning("download failed for %s", path, exc_info=True)
                files[path] = ""
                continue
            self._downloaded_files[path] = raw
            files[path] = raw.decode("utf-8", errors="replace")
        return files

    async def run(self) -> AnalyzerOutput:
        """Provision, run, and close this block's backend client."""
        self.job_started_at = utcnow()
        self.status = JobStatus.RUNNING
        self.log.info("block starting (llm_client_type=%s)", self.llm_client_type.value)
        try:
            self._active_client = await self._create_client()
            sandbox_id = getattr(self._active_client, "sandbox_id", None)
            if sandbox_id and self._on_chunk is not None:
                # Tell the progress hook which sandbox this block runs in,
                # in the same shape as the streamed events.
                try:
                    self._on_chunk(
                        json.dumps({"type": "sandbox_info", "sandbox_id": sandbox_id})
                    )
                except Exception:  # noqa: BLE001
                    self.log.exception("on_chunk hook failed")
            async for _ in self.stream_output():
                pass
            if self.input.files_to_download:
                self.output = AnalyzerOutput(
                    output=await self._download_requested_files()
                )
            else:
                raw = "".join(self._chunks)
                self.output = AnalyzerOutput(
                    output=(
                        self._output_transform(raw) if self._output_transform else raw
                    )
                )
            self.status = JobStatus.SUCCESS
            self.log.info("block succeeded (%d chunk(s))", len(self._chunks))
            return self.output
        except BaseException as exc:  # incl. asyncio.CancelledError
            self.status = JobStatus.FAILED
            self.error = repr(exc)
            self.log.exception("block failed")
            raise
        finally:
            if self._active_client is not None:
                self.usage = getattr(self._active_client, "last_usage", None)
                try:
                    # Bounded: aclose() deletes the sandbox over the network, and
                    # an unbounded await here outlives the caller's own timeout.
                    # On expiry the sandbox falls back to Daytona's auto-delete.
                    await asyncio.wait_for(
                        self._active_client.aclose(),
                        timeout=self._client_close_timeout,
                    )
                except Exception:  # noqa: BLE001
                    self.log.exception("client cleanup failed")
                self._active_client = None
            self.job_ended_at = utcnow()
            if self.job_started_at is not None:
                self.job_duration_seconds = (
                    self.job_ended_at - self.job_started_at
                ).total_seconds()
            # Guarantee the save runs to completion even when run() is being
            # cancelled. A bare ``await asyncio.shield(...)`` is not enough: if
            # our await is itself cancelled, the shielded task keeps running but
            # we'd unwind before it finishes -- dropping the save. So hold the
            # task handle and, on cancellation, wait for it before re-raising.
            persist = asyncio.ensure_future(self._persist())
            try:
                await asyncio.shield(persist)
            except asyncio.CancelledError:
                await persist
                raise

    async def _persist(self) -> None:
        """S3, then the cost row, then the DB. Each is failure-isolated inside
        its own method, so an S3 outage still lets the DB row land (and vice
        versa). Cost goes before the DB write so ``cost_status`` is on the row
        that lands -- stamping it afterwards would never reach Postgres."""
        raw = "".join(self._chunks).encode("utf-8")
        await self.save_to_s3(raw)
        await self.record_cost()
        await self.save_to_db()
