"""Parse one cohort agent's output into (findings, sections, proposals).

Files are the source of truth; the stream markers are the fallback for when the
agent never wrote them. Pure: callers hand in bytes and text.
"""

from __future__ import annotations

import json
import logging

from oddish.evals.analyzer.core import parse_json
from oddish.evals.analyzer.prompt_builder import SECTION_KEYS_BY_BUCKET
from oddish.evals.analyzer.schemas import CapabilityProposal, Finding

logger = logging.getLogger(__name__)

_MAP_MARKER = "MAP FINDING:"
_REDUCE_MARKER = "REDUCE RESULT:"
_DECODER = json.JSONDecoder()


class CohortParseError(RuntimeError):
    """Neither the output file nor the stream yielded a usable reduce result."""


def _finding_from(d: dict, bucket: str, host_by_trial: dict[str, dict]) -> Finding | None:
    trial_id = d.get("trial_id", "")
    host = host_by_trial.get(trial_id)
    if host is None:
        logger.warning(
            "analyzer-sandbox: dropping finding for trial %r not in the %s cohort",
            trial_id, bucket,
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
        # Good bucket only: the bad bucket classifies task defects, not agent
        # capabilities. Gating here (not in the prompt) means drift can't leak.
        capability_slug=(d.get("capability_slug") or None) if bucket == "good" else None,
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


def _merge_proposals(
    dicts: list[dict], bucket: str, host_by_trial: dict[str, dict]
) -> list[CapabilityProposal]:
    """Merge by slug. One cohort is one continuous session, and a single
    session can still re-propose the same capability across several similar
    trials, so dedup is on the model's output, not on process boundaries."""
    if bucket != "good":
        return []
    merged: dict[str, CapabilityProposal] = {}
    for d in dicts:
        raw = d.get("capability_proposal")
        slug = d.get("capability_slug")
        trial_id = d.get("trial_id", "")
        if not isinstance(raw, dict) or not slug or trial_id not in host_by_trial:
            continue
        existing = merged.get(slug)
        if existing is None:
            merged[slug] = CapabilityProposal(
                slug=slug,
                name=raw.get("name", slug),
                description=raw.get("description", ""),
                example=raw.get("example", ""),
                categories=list(raw.get("categories") or []),
                trial_ids=[trial_id],
                trajectory_link=host_by_trial[trial_id]["trajectory_link"],
            )
        elif trial_id not in existing.trial_ids:
            existing.trial_ids.append(trial_id)
    return list(merged.values())


def _proposals_from(
    text: str, bucket: str, host_by_trial: dict[str, dict]
) -> list[CapabilityProposal]:
    dicts = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            dicts.append(json.loads(line))
        except ValueError:
            continue
    return _merge_proposals(dicts, bucket, host_by_trial)


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


def parse_cohort_result(
    bucket: str,
    reduce_bytes: bytes,
    findings_bytes: bytes,
    stream_text: str,
    host_by_trial: dict[str, dict],
) -> tuple[list[Finding], dict[str, str], list[CapabilityProposal]]:
    findings_text = findings_bytes.decode("utf-8", "replace")
    findings = _findings_from_jsonl(findings_text, bucket, host_by_trial)
    proposals = _proposals_from(findings_text, bucket, host_by_trial)

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
        blocks = _marked(stream_text, _REDUCE_MARKER)
        if not blocks:
            raise CohortParseError(
                f"no usable reduce result for bucket {bucket!r}: "
                f"file was {len(reduce_bytes)}B and the stream had no "
                f"{_REDUCE_MARKER!r} marker"
            )
        sections = _sections_from(blocks[-1], bucket)

    if not findings:
        map_dicts = _marked(stream_text, _MAP_MARKER)
        findings = [
            f for d in map_dicts
            if (f := _finding_from(d, bucket, host_by_trial)) is not None
        ]
        proposals = _merge_proposals(map_dicts, bucket, host_by_trial)
    return findings, sections, proposals
