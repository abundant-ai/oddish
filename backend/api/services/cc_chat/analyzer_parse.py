"""Parse one cohort agent's output into (findings, sections).

Files are the source of truth; the stream markers are the fallback for when the
agent never wrote them. Pure: callers hand in bytes and text.
"""

from __future__ import annotations

import json
import logging

from api.services.cc_chat.stream_render import event_texts
from oddish.evals.analyzer.core import parse_json
from oddish.evals.analyzer.prompt_builder import SECTION_KEYS_BY_BUCKET
from oddish.evals.analyzer.schemas import Finding

logger = logging.getLogger(__name__)

_MAP_MARKER = "MAP FINDING:"
_REDUCE_MARKER = "REDUCE RESULT:"


def marker_payloads(evt: dict) -> list[str]:
    """The marker-bearing text payloads of one stream event, untruncated.

    Whole payloads, not just the matching lines, so a reduce object pretty-printed
    under its marker survives intact.
    """
    return [
        t for t in event_texts(evt)
        if _MAP_MARKER in t or _REDUCE_MARKER in t
    ]


class CohortParseError(RuntimeError):
    """Neither the output file nor the stream yielded a usable reduce result."""


def _finding_from(d: dict, bucket: str, link_by_trial: dict[str, str]) -> Finding | None:
    trial_id = d.get("trial_id", "")
    link = link_by_trial.get(trial_id)
    if link is None:
        logger.warning(
            "analyzer-sandbox: dropping finding for trial %r not in the %s cohort",
            trial_id, bucket,
        )
        return None
    return Finding(
        trial_id=trial_id,
        # Never trust the model's echo, same rule as trajectory_link below.
        bucket=bucket,
        subcategory=d.get("subcategory", "emergent"),
        evidence_quote=d.get("evidence_quote", ""),
        step_ids=list(d.get("step_ids") or []),
        root_cause=d.get("root_cause", ""),
        headroom_signal=d.get("headroom_signal", ""),
        # Trust the host-built link, never the model's echo.
        trajectory_link=link,
    )


def _section_value(raw: dict, key: str) -> str:
    v = raw.get(key, "")
    if not isinstance(v, str):
        if key in raw:
            logger.warning(
                "analyzer-sandbox: section %r had non-string value of type %s; "
                "treating as absent", key, type(v).__name__,
            )
        return ""
    return v


def _sections_from(raw: dict, bucket: str) -> dict[str, str]:
    keys = SECTION_KEYS_BY_BUCKET[bucket]
    sections = {k: _section_value(raw, k) for k in keys}
    if not any(v.strip() for v in sections.values()):
        raise CohortParseError(
            f"reduce output for bucket {bucket!r} has no non-blank content for "
            f"any of {keys}: {sorted(raw)[:6]}"
        )
    return sections


def _findings_from_jsonl(text: str, bucket, links) -> list[Finding]:
    out = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            d = json.loads(line)
        except ValueError:
            logger.warning("analyzer-sandbox: skipping unparseable finding line")
            continue
        f = _finding_from(d, bucket, links)
        if f is not None:
            out.append(f)
    return out


def _marked(stream_text: str, marker: str) -> list[dict]:
    out = []
    for line in stream_text.splitlines():
        idx = line.find(marker)
        if idx == -1:
            continue
        try:
            out.append(parse_json(line[idx + len(marker):]))
        except ValueError:
            continue
    return out


def _marked_reduce(stream_text: str) -> list[dict]:
    """Per-line scan is primary; whole-text-from-last-marker is the net for
    agents that pretty-print the reduce object across several lines."""
    blocks = _marked(stream_text, _REDUCE_MARKER)
    if blocks:
        return blocks
    idx = stream_text.rfind(_REDUCE_MARKER)
    if idx == -1:
        return []
    try:
        return [parse_json(stream_text[idx + len(_REDUCE_MARKER):])]
    except ValueError:
        return []


def parse_cohort_result(
    bucket: str,
    reduce_bytes: bytes,
    findings_bytes: bytes,
    stream_text: str,
    link_by_trial: dict[str, str],
) -> tuple[list[Finding], dict[str, str]]:
    findings = _findings_from_jsonl(
        findings_bytes.decode("utf-8", "replace"), bucket, link_by_trial
    )

    sections: dict[str, str] | None = None
    if reduce_bytes.strip():
        try:
            sections = _sections_from(
                parse_json(reduce_bytes.decode("utf-8", "replace")), bucket
            )
        except (CohortParseError, ValueError) as exc:
            # Unreadable JSON and well-formed-but-empty JSON are the same situation
            # to the caller: the file yielded nothing. Both fall back to the stream,
            # which may still hold a good result. If it doesn't, the check below
            # raises with both channels accounted for.
            logger.warning(
                "analyzer-sandbox: %s reduce file unusable (%s); "
                "falling back to the stream", bucket, exc,
            )

    if sections is None:
        blocks = _marked_reduce(stream_text)
        if not blocks:
            raise CohortParseError(
                f"no usable reduce result for bucket {bucket!r}: "
                f"file was {len(reduce_bytes)}B and the stream capture yielded no "
                f"parseable {_REDUCE_MARKER!r} object"
            )
        sections = _sections_from(blocks[-1], bucket)

    if not findings:
        # An agent that narrates a finding and later cats the file back emits the
        # same marker twice; one trial gets one finding, and the narrated one wins.
        seen: set[str] = set()
        for d in _marked(stream_text, _MAP_MARKER):
            f = _finding_from(d, bucket, link_by_trial)
            if f is not None and f.trial_id not in seen:
                seen.add(f.trial_id)
                findings.append(f)
    return findings, sections
