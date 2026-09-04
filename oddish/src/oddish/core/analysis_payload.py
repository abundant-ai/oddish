from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from oddish.db import TaskVersionModel


def audit_fingerprint(version: TaskVersionModel) -> str:
    """Pin source bytes and an audit attempt, excluding later exploitation annotations."""
    audit = version.pre_trial
    if isinstance(audit, dict):
        audit = {
            **audit,
            "items": [
                {
                    k: v
                    for k, v in item.items()
                    if k not in {"exploited", "exploit_evidence"}
                }
                for item in audit.get("items", [])
            ],
        }
    encoded = json.dumps(
        [
            version.id,
            version.content_hash,
            version.pre_trial_status,
            version.pre_trial_started_at,
            version.pre_trial_finished_at,
            audit,
        ],
        sort_keys=True,
        default=str,
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


def audit_snapshot_matches(version: TaskVersionModel | None, payload: dict) -> bool:
    """New QA jobs pin the whole audit; legacy jobs must match their saved finding IDs."""
    # A matching snapshot of an unfinished audit still cannot authorize QA.
    if (
        version is not None
        and version.pre_trial_status is not None
        and version.pre_trial_status.value
        in {
            "queued",
            "running",
            "pending",
        }
    ):
        return False
    pinned = payload.get("audit_fingerprint")
    if pinned is not None:
        return version is not None and pinned == audit_fingerprint(version)
    if version is None:
        return not payload.get("pre_trial_item_ids")
    # Existing jobs have no fingerprint; require their saved findings to match.
    items = (version.pre_trial or {}).get("items", [])
    items = [item for item in items if isinstance(item, dict) and item.get("id")]
    return {str(item["id"]) for item in items} == set(
        payload.get("pre_trial_item_ids", [])
    ) and {
        str(item["id"])
        for item in items
        if item.get("tier", item.get("severity")) == "must_fix"
    } == set(payload.get("pre_trial_must_fix_ids", []))


class AnalysisPayloadError(ValueError):
    """The stored analysis payload cannot authorize or validate its trial."""


def _string_ids(value: object, *, field: str) -> tuple[str, ...]:
    if not isinstance(value, list) or any(
        not isinstance(item, str) or not item.strip() for item in value
    ):
        raise AnalysisPayloadError(f"{field} must be a list of non-empty strings")
    normalized = tuple(item.strip() for item in value)
    if len(normalized) != len(set(normalized)):
        raise AnalysisPayloadError(f"{field} must not contain duplicates")
    return normalized


@dataclass(frozen=True, slots=True)
class ParsedAnalysisPayload:
    kind: str
    trial_ids: tuple[str, ...] = ()
    trial_evidence: tuple[dict, ...] = ()
    baseline_evidence: tuple[dict, ...] = ()
    pre_trial_item_ids: tuple[str, ...] = ()
    pre_trial_must_fix_ids: tuple[str, ...] = ()
    with_verdict: bool = True
    target_trial_id: str | None = None
    task_version_content_hash: str | None = None
    audit_policy_hash: str | None = None
    audit_fingerprint: str | None = None


def parse_analysis_payload(
    kind: str,
    harbor_config: dict | None,
) -> ParsedAnalysisPayload:
    """Validate stored analysis JSON once at its persistence boundary."""
    if kind not in ("qa", "qa_eval", "audit", "summarize"):
        raise AnalysisPayloadError(f"unsupported analysis trial kind {kind!r}")

    payload = (harbor_config or {}).get("analysis_payload")
    if kind == "audit" and payload is None:
        # Audit payloads predate content-hash pinning. A missing object means
        # there is no pin, not that the audit contract is malformed.
        return ParsedAnalysisPayload(kind=kind)
    if not isinstance(payload, dict):
        raise AnalysisPayloadError(
            f"{kind} harbor_config.analysis_payload must be an object"
        )

    if kind == "audit":
        content_hash = payload.get("task_version_content_hash")
        if content_hash is not None and (
            not isinstance(content_hash, str) or not content_hash.strip()
        ):
            raise AnalysisPayloadError(
                "audit analysis_payload.task_version_content_hash must be a "
                "non-empty string when present"
            )
        policy_hash = payload.get("audit_policy_hash")
        if policy_hash is not None and (
            not isinstance(policy_hash, str)
            or len(policy_hash) != 64
            or any(c not in "0123456789abcdef" for c in policy_hash)
        ):
            raise AnalysisPayloadError(
                "audit analysis_payload.audit_policy_hash must be a SHA-256 hex digest"
            )
        return ParsedAnalysisPayload(
            kind=kind,
            task_version_content_hash=(
                content_hash.strip() if isinstance(content_hash, str) else None
            ),
            audit_policy_hash=policy_hash,
        )

    if kind in ("qa", "qa_eval"):
        trial_ids = _string_ids(
            payload.get("trial_ids"), field=f"{kind} analysis_payload.trial_ids"
        )
        if not trial_ids:
            raise AnalysisPayloadError(
                f"{kind} analysis_payload.trial_ids must not be empty"
            )
        if kind == "qa_eval" and len(trial_ids) != 1:
            raise AnalysisPayloadError(
                "qa_eval analysis_payload.trial_ids must contain exactly one "
                f"source trial id; found {len(trial_ids)}"
            )

        trial_evidence = payload.get("trial_evidence", [])
        baseline_evidence = payload.get("baseline_evidence", [])
        if not isinstance(trial_evidence, list) or not all(
            isinstance(item, dict) for item in trial_evidence
        ):
            raise AnalysisPayloadError(
                f"{kind} analysis_payload.trial_evidence must be a list of objects"
            )
        if not isinstance(baseline_evidence, list) or not all(
            isinstance(item, dict) for item in baseline_evidence
        ):
            raise AnalysisPayloadError(
                f"{kind} analysis_payload.baseline_evidence must be a list of objects"
            )
        if baseline_evidence:
            _string_ids(
                [item.get("trial_id") for item in baseline_evidence],
                field=f"{kind} analysis_payload.baseline_evidence trial ids",
            )
        if trial_evidence:
            evidence_ids = _string_ids(
                [item.get("trial_id") for item in trial_evidence],
                field=f"{kind} analysis_payload.trial_evidence trial ids",
            )
            if set(evidence_ids) != set(trial_ids):
                raise AnalysisPayloadError(
                    f"{kind} analysis_payload.trial_evidence must cover trial_ids exactly"
                )

        pre_trial_item_ids = _string_ids(
            payload.get("pre_trial_item_ids", []),
            field=f"{kind} analysis_payload.pre_trial_item_ids",
        )
        pre_trial_must_fix_ids = _string_ids(
            payload.get("pre_trial_must_fix_ids", []),
            field=f"{kind} analysis_payload.pre_trial_must_fix_ids",
        )
        if not set(pre_trial_must_fix_ids).issubset(pre_trial_item_ids):
            raise AnalysisPayloadError(
                f"{kind} analysis_payload.pre_trial_must_fix_ids must be a subset "
                "of pre_trial_item_ids"
            )
        with_verdict = payload.get("with_verdict", True)
        if not isinstance(with_verdict, bool):
            raise AnalysisPayloadError(
                f"{kind} analysis_payload.with_verdict must be a boolean"
            )
        fingerprint = payload.get("audit_fingerprint")
        if fingerprint is not None and (
            not isinstance(fingerprint, str)
            or len(fingerprint) != 64
            or any(c not in "0123456789abcdef" for c in fingerprint)
        ):
            raise AnalysisPayloadError("audit_fingerprint must be a SHA-256 hex digest")
        return ParsedAnalysisPayload(
            kind=kind,
            trial_ids=trial_ids,
            trial_evidence=tuple(trial_evidence),
            baseline_evidence=tuple(baseline_evidence),
            pre_trial_item_ids=pre_trial_item_ids,
            pre_trial_must_fix_ids=pre_trial_must_fix_ids,
            with_verdict=with_verdict,
            audit_fingerprint=fingerprint,
        )

    if kind == "summarize":
        target_trial_id = payload.get("target_trial_id")
        if not isinstance(target_trial_id, str) or not target_trial_id.strip():
            raise AnalysisPayloadError(
                "summarize analysis_payload.target_trial_id must be a non-empty string"
            )
        return ParsedAnalysisPayload(
            kind=kind,
            target_trial_id=target_trial_id.strip(),
        )

    raise AssertionError(f"unhandled analysis trial kind {kind!r}")


def analysis_source_trial_ids(
    kind: str,
    harbor_config: dict | None,
) -> tuple[str, ...]:
    """Return the source trials owned by a QA or QA-eval analysis run."""
    if kind not in ("qa", "qa_eval"):
        return ()
    return parse_analysis_payload(kind, harbor_config).trial_ids
