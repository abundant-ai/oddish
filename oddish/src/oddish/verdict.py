"""Public vocabulary for the task judgment, independent of generation diagnostics."""

from collections.abc import Mapping
from typing import Any


def verdict_label(
    status: str | None,
    verdict: Mapping[str, Any] | None,
    *,
    version_matches: bool | None = None,
) -> str:
    """Never present a failed, replaced, or inconclusive judgment as acceptance."""
    if status in {"pending", "queued"}:
        return "Verdict queued"
    if status == "running":
        return "Verdict running"
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
