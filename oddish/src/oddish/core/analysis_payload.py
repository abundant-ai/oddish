from __future__ import annotations


class AnalysisPayloadError(ValueError):
    """The stored analysis payload cannot authorize or validate its trial."""


def analysis_source_trial_ids(
    kind: str,
    harbor_config: dict | None,
) -> tuple[str, ...]:
    """Return the source trials owned by a QA or QA-eval analysis run."""
    if kind not in ("qa", "qa_eval"):
        return ()

    payload = (harbor_config or {}).get("analysis_payload")
    trial_ids = payload.get("trial_ids") if isinstance(payload, dict) else None
    if not isinstance(trial_ids, list) or any(
        not isinstance(trial_id, str) or not trial_id.strip() for trial_id in trial_ids
    ):
        raise AnalysisPayloadError(
            f"{kind} analysis_payload.trial_ids must be a list of non-empty strings"
        )

    normalized = tuple(trial_id.strip() for trial_id in trial_ids)
    if kind == "qa_eval" and len(normalized) != 1:
        raise AnalysisPayloadError(
            "qa_eval analysis_payload.trial_ids must contain exactly one "
            f"source trial id; found {len(normalized)}"
        )
    return normalized
