"""Public vocabulary for the task judgment, independent of generation diagnostics."""

from collections.abc import Mapping
from typing import Any


def verdict_label(
    status: str | None,
    verdict: Mapping[str, Any] | None,
    *,
    version_matches: bool | None = None,
    standalone: bool = True,
) -> str:
    """Use short progress values when the surrounding heading already says Verdict."""
    if status in {"pending", "queued"}:
        return "Verdict queued" if standalone else "Queued"
    if status == "running":
        return "Verdict running" if standalone else "Running"
    if status == "failed" or version_matches is False:
        return "No verdict"
    if verdict is not None:
        label = verdict.get("verdict")
        if label == "accept":
            return "Accepted"
        if label == "reject":
            return "Rejected"
        if verdict.get("is_good") is True:
            return "Accepted"
        if verdict.get("is_good") is False:
            return "Rejected"
    return "No verdict"
