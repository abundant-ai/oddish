"""Parse one cohort agent's output into (findings, sections).

Files are the source of truth; the stream markers are the fallback for when the
agent never wrote them. Pure: callers hand in bytes and text.
"""

from __future__ import annotations

import json
import logging

from oddish.evals.analyzer.core import parse_json
from oddish.evals.analyzer.prompt_builder import SECTION_KEYS_BY_BUCKET
from oddish.evals.analyzer.schemas import Finding
from oddish.src.oddish.db import get_storage_client
from backend.api.services.helpers import raw_bytes

logger = logging.getLogger(__name__)

_MAP_MARKER = "MAP FINDING:"
_REDUCE_MARKER = "REDUCE RESULT:"
_DECODER = json.JSONDecoder()
_SECTION_LOG_LIMIT = 200


class CohortParseError(RuntimeError):
    """Neither the output file nor the stream yielded a usable reduce result."""


def _finding_from(
    d: dict, bucket: str, host_by_trial: dict[str, dict]
) -> Finding | None:
    trial_id = d.get("trial_id", "")
    host = host_by_trial.get(trial_id)
    if host is None:
        logger.warning(
            "analyzer-sandbox: dropping finding for trial %r not in the %s cohort",
            trial_id,
            bucket,
        )
        return None
    return Finding(
        trial_id=trial_id,
        # Never trust the model's echo, same rule as the host facts below.
        bucket=bucket,
        subcategory=d.get("subcategory", "emergent"),
        evidence_quote=d.get("evidence_quote", ""),
        step_ids=list(d.get("step_ids") or []),
        root_cause=d.get("root_cause", ""),
        headroom_signal=d.get("headroom_signal", ""),
        # Host facts (link + classifier columns), never the model's echo -- the
        # rollup derives lanes from these instead of the bucket/subcategory above.
        **host,
    )


def _section_value(raw: dict, key: str) -> str:
    v = raw.get(key, "")
    if not isinstance(v, str):
        if key in raw:
            logger.warning(
                "analyzer-sandbox: section %r had non-string value of type %s; "
                "treating as absent",
                key,
                type(v).__name__,
            )
        return ""
    return v


def _render_sections(sections: dict[str, str]) -> str:
    """Compact key -> value dump for error text; values are elided so a runaway
    reduce output can't turn one exception into a megabyte of log."""
    return ", ".join(f"{k}={_elide(v)!r}" for k, v in sections.items()) or "<none>"


def _elide(v: str, limit: int = _SECTION_LOG_LIMIT) -> str:
    return v if len(v) <= limit else f"{v[:limit]}...(+{len(v) - limit}B)"


def _sections_from(raw: dict, bucket: str) -> dict[str, str]:
    keys = SECTION_KEYS_BY_BUCKET[bucket]
    sections = {k: _section_value(raw, k) for k in keys}
    if not any(v.strip() for v in sections.values()):
        raise CohortParseError(
            f"reduce output for bucket {bucket!r} has no non-blank content for "
            f"any of {keys}: sections={_render_sections(sections)}; "
            f"raw keys={sorted(raw)[:6]}"
        )
    return sections


def _findings_from_jsonl(text: str, bucket, links) -> list[Finding]:
    out = []
    for lineno, line in enumerate(text.splitlines(), 1):
        line = line.strip()
        if not line:
            continue
        try:
            d = json.loads(line)
        except ValueError:
            logger.warning(
                "analyzer-sandbox: skipping unparseable finding line %d: %r",
                lineno,
                _elide(line),
            )
            continue
        f = _finding_from(d, bucket, links)
        if f is not None:
            out.append(f)
    return out


def _marked(stream_text: str, marker: str) -> list[dict]:
    """Decode one object per marker, spanning lines if the agent pretty-printed
    it. raw_decode stops at the end of that object, so narration (or a later
    marker) after it is left alone -- which a scan for the outermost braces
    would swallow whole.
    """
    out: list[dict] = []
    pos = stream_text.find(marker)
    while pos != -1:
        after = pos + len(marker)
        start = stream_text.find("{", after)
        if start == -1:
            break
        try:
            obj, end = _DECODER.raw_decode(stream_text, start)
        except ValueError:
            pos = stream_text.find(marker, after)
            continue
        if isinstance(obj, dict):
            out.append(obj)
        pos = stream_text.find(marker, end)
    return out


async def parse_and_save_cohort_result(
    bucket: str,
    reduce_bytes: bytes,
    findings_bytes: bytes,
    stream_text: str,
    host_by_trial: dict[str, dict],
    analyzer_id: str,
) -> tuple[list[Finding], dict[str, str]]:
    logger.info(
        "analyzer-sandbox: parsing %s cohort: reduce file=%dB, findings file=%dB, "
        "stream=%dB, %d trials in cohort",
        bucket,
        len(reduce_bytes),
        len(findings_bytes),
        len(stream_text),
        len(host_by_trial),
    )

    storage = get_storage_client()
    prefix = f"analyzer/sandbox/{analyzer_id}"
    s3_key = f"prefix/{analyzer_id}"
    data = raw_bytes(stream_text)
    if data:
        await storage.upload_bytes(
            data,
            s3_key,
            content_type="application/x-ndjson",
        )

    findings = _findings_from_jsonl(
        findings_bytes.decode("utf-8", "replace"), bucket, host_by_trial
    )
    logger.info(
        "analyzer-sandbox: %s findings file yielded %d finding(s)",
        bucket,
        len(findings),
    )

    sections: dict[str, str] | None = None
    if reduce_bytes.strip():
        try:
            sections = _sections_from(
                parse_json(reduce_bytes.decode("utf-8", "replace")), bucket
            )
            logger.info(
                "analyzer-sandbox: %s reduce file parsed: sections=%s",
                bucket,
                _render_sections(sections),
            )
        except (CohortParseError, ValueError) as exc:
            # Unreadable JSON and well-formed-but-empty JSON are the same situation
            # to the caller: the file yielded nothing. Both fall back to the stream,
            # which may still hold a good result. If it doesn't, the check below
            # raises with both channels accounted for.
            logger.warning(
                "analyzer-sandbox: %s reduce file unusable (%s); "
                "falling back to the stream",
                bucket,
                exc,
            )
    else:
        logger.info(
            "analyzer-sandbox: %s reduce file is blank; falling back to the stream",
            bucket,
        )

    if sections is None:
        blocks = _marked(stream_text, _REDUCE_MARKER)
        logger.info(
            "analyzer-sandbox: %s stream had %d %r block(s)",
            bucket,
            len(blocks),
            _REDUCE_MARKER,
        )
        if not blocks:
            raise CohortParseError(
                f"no usable reduce result for bucket {bucket!r}: "
                f"file was {len(reduce_bytes)}B and the stream had no "
                f"{_REDUCE_MARKER!r} marker; expected sections "
                f"{SECTION_KEYS_BY_BUCKET[bucket]}; "
                f"file head={_elide(reduce_bytes.decode('utf-8', 'replace'))!r}; "
                f"stream tail={_elide(stream_text[-_SECTION_LOG_LIMIT:])!r}"
            )
        sections = _sections_from(blocks[-1], bucket)
        logger.info(
            "analyzer-sandbox: %s reduce recovered from the stream: sections=%s",
            bucket,
            _render_sections(sections),
        )

    if not findings:
        blocks = _marked(stream_text, _MAP_MARKER)
        findings = [
            f
            for d in blocks
            if (f := _finding_from(d, bucket, host_by_trial)) is not None
        ]
        logger.info(
            "analyzer-sandbox: %s findings recovered from the stream: %d of %d "
            "%r block(s) kept",
            bucket,
            len(findings),
            len(blocks),
            _MAP_MARKER,
        )

    logger.info(
        "analyzer-sandbox: %s parsed: %d finding(s), %d section(s)",
        bucket,
        len(findings),
        len(sections),
    )
    return findings, sections
