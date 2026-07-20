from __future__ import annotations

import asyncio
import enum
import logging
from dataclasses import dataclass
from typing import Any, Callable

from oddish.db import generate_id, get_session
from oddish.db.models import AnalyzerBlockModel, JobStatus, utcnow
from oddish.db.storage import get_storage_client

from api.services.blocks.analyzer.analyzer_llm_client import (
    AnalyzerLLMClient,
    LLMClientType,
    create_llm_client,
)
from api.services.blocks.block import Block


class AnalyzerType(str, enum.Enum):
    TRAJECTORY_FAILURE_ANALYSIS = "trajectory_failure_analysis"
    HEADROOM_ANALYSIS = "headroom_analysis"
    SCALING_ANALYSIS = "scaling_analysis"
    TRAJECTORY_SUMMARY = "trajectory_summary"


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
    def process(self, msg: str, kwargs: dict) -> tuple[str, dict]:
        return f"[{self.extra['prefix']}] {msg}", kwargs


def block_logger(key_prefix: str) -> logging.LoggerAdapter:
    """A logger whose every record (including exceptions) is tagged with the
    block's key_prefix, so all of one block's output is greppable by type."""
    return _PrefixAdapter(
        logging.getLogger("oddish.analyzer_block"), {"prefix": key_prefix}
    )


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
        block_metadata: dict | None = None,
        client: AnalyzerLLMClient | None = None,
        system_prompt: str | None = None,
        model: str | None = None,
        output_transform: Callable[[str], Any] | None = None,
        api_key: str | None = None,
    ) -> None:
        self.id = generate_id()
        self.analyzer_type = analyzer_type
        self.llm_client_type = llm_client_type
        self.input = input
        self.prompt = prompt
        self.analyzer_id = analyzer_id
        self._client = client
        if input.files_to_download:
            if llm_client_type == LLMClientType.API:
                raise ValueError(
                    "files_to_download is sandbox-only; the API backend has no filesystem"
                )
            if client is None:
                raise ValueError(
                    "files_to_download requires an injected client (a self-provisioned "
                    "block deletes its sandbox before run() can download)"
                )
        self._downloaded_files: dict[str, bytes] = {}
        self.system_prompt = system_prompt
        self.model = model
        if model:
            block_metadata = {**(block_metadata or {}), "model": model}
        self.block_metadata = block_metadata
        self._output_transform = output_transform
        # Only used when self-provisioning (client is None): which Anthropic key
        # the backend gets. None -> the analyzer key / global default.
        self._api_key = api_key

        self.key_prefix = block_key_prefix(analyzer_type)
        self.log = block_logger(self.key_prefix)

        self.status: JobStatus = JobStatus.PENDING
        self.output: AnalyzerOutput | None = None
        self.error: str | None = None
        self.job_started_at = None
        self.job_ended_at = None
        self.job_duration_seconds: float | None = None
        self._chunks: list[str] = []

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
                        id=self.id,
                        analyzer_id=self.analyzer_id,
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
                        block_metadata=self.block_metadata,
                    )
                )
            self.log.info("saved block row id=%s status=%s", self.id, self.status.value)
        except Exception:
            self.log.exception("save_to_db failed for id=%s", self.id)

    async def stream_output(self):
        """Yield each output chunk to the caller and accumulate it. Lazily
        provisions the backend client (or uses the injected one)."""
        client = self._client or await create_llm_client(
            self.llm_client_type, api_key=self._api_key
        )
        try:
            async for chunk in client.stream(
                self.prompt, system_prompt=self.system_prompt
            ):
                self._chunks.append(chunk)
                self.log.debug("chunk %d (len=%d)", len(self._chunks), len(chunk))
                yield chunk
        finally:
            # Only close a client we created; an injected one is the caller's.
            if self._client is None:
                await client.aclose()

    async def run(self) -> AnalyzerOutput:
        """Drive the stream to completion, persisting on every exit path."""
        self.job_started_at = utcnow()
        self.status = JobStatus.RUNNING
        self.log.info("block starting (llm_client_type=%s)", self.llm_client_type.value)
        try:
            async for _ in self.stream_output():
                pass
            raw = "".join(self._chunks)
            self.output = AnalyzerOutput(
                output=self._output_transform(raw) if self._output_transform else raw
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
        """S3 first, then DB. Each is failure-isolated inside its own method, so
        an S3 outage still lets the DB row land (and vice versa)."""
        raw = "".join(self._chunks).encode("utf-8")
        await self.save_to_s3(raw)
        await self.save_to_db()
