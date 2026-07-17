from __future__ import annotations

import enum
import logging
from dataclasses import dataclass
from typing import Any

from oddish.db import generate_id, get_session
from oddish.db.models import AnalyzerBlockModel, JobStatus, utcnow
from oddish.db.storage import get_storage_client

from api.services.analyzer_llm_client import (
    AnalyzerLLMClient,
    LLMClientType,
    create_llm_client,
)


class AnalyzerType(str, enum.Enum):
    TRAJECTORY_FAILURE_ANALYSIS = "trajectory_failure_analysis"
    HEADROOM_ANALYSIS = "headroom_analysis"
    SCALING_ANALYSIS = "scaling_analysis"


@dataclass
class AnalyzerInput:
    input: Any


@dataclass
class AnalyzerOutput:
    output: Any


def block_key_prefix(analyzer_type: AnalyzerType) -> str:
    """S3 prefix / log tag for a block, keyed by its analyzer type."""
    return f"analyzer/{analyzer_type.value}"


class _PrefixAdapter(logging.LoggerAdapter):
    def process(self, msg: str, kwargs: dict) -> tuple[str, dict]:
        return f"[{self.extra['prefix']}] {msg}", kwargs


def block_logger(key_prefix: str) -> logging.LoggerAdapter:
    """A logger whose every record (including exceptions) is tagged with the
    block's key_prefix, so all of one block's output is greppable by type."""
    return _PrefixAdapter(logging.getLogger("oddish.analyzer_block"), {"prefix": key_prefix})


class AnalyzerBlock:
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
    ) -> None:
        self.id = generate_id()
        self.analyzer_type = analyzer_type
        self.llm_client_type = llm_client_type
        self.input = input
        self.prompt = prompt
        self.analyzer_id = analyzer_id
        self.block_metadata = block_metadata
        self._client = client

        self.key_prefix = block_key_prefix(analyzer_type)
        self.log = block_logger(self.key_prefix)

        self.status: JobStatus = JobStatus.PENDING
        self.output: AnalyzerOutput | None = None
        self.error: str | None = None
        self.job_started_at = None
        self.job_ended_at = None
        self.job_duration_seconds: float | None = None

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
