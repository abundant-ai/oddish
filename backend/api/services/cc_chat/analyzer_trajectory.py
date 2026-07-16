"""Persist one analyzer sandbox turn's raw trajectory to S3.

Raw stream-json events, not render_event's output: rendering truncates tool
inputs to 200 chars and drops _stderr/_invalid_json frames entirely, so the
evidence for whether an agent widened --tail-bytes survives only in the raw
event. Rendering can be regenerated from raw; not the reverse.
"""

from __future__ import annotations

import json
import logging

from oddish.config import settings
from oddish.db.storage import get_storage_client

logger = logging.getLogger(__name__)

REDUCE_SLUG = "reduce"


def trajectory_key(analyzer_id: str, bucket: str, slug: str) -> str:
    return f"analyzers/{analyzer_id}/{bucket}/{slug}.jsonl"


def map_slug(batch_no: int) -> str:
    # Zero-padded for the reason findings_path already documents: unpadded, 10
    # sorts before 2.
    return f"map-{batch_no:02d}"


async def persist_turn(
    *,
    analyzer_id: str,
    bucket: str,
    slug: str,
    events: list[dict],
) -> None:
    """Best-effort upload of one turn's raw events.

    A failure logs a warning but must NOT fail the analyzer job -- the findings
    are the primary product. Catches Exception, never BaseException: this runs
    inside asyncio.timeout(COHORT_TIMEOUT_SECONDS), and swallowing
    CancelledError would break the cohort timeout.
    """
    if not events:
        logger.warning(
            "analyzer-trajectory: %s/%s produced no events; nothing to upload",
            bucket, slug,
        )
        return
    key = trajectory_key(analyzer_id, bucket, slug)
    try:
        data = "\n".join(json.dumps(e) for e in events).encode("utf-8")
        await get_storage_client().upload_bytes(
            data, key, content_type="application/x-ndjson"
        )
        logger.info(
            "analyzer-trajectory: saved %d events to s3://%s/%s",
            len(events), settings.s3_bucket, key,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "analyzer-trajectory: failed to save %d events to s3://%s/%s: %s",
            len(events), settings.s3_bucket, key, exc,
        )
