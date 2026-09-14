"""Allowlisted projections for task data sent to anonymous clients."""

from collections.abc import Mapping

PUBLIC_TASK_GITHUB_META_KEYS = frozenset(
    {
        "category",
        "task_category",
        "benchmark_category",
        "track_category",
        "world",
        "task_world",
        "benchmark_world",
        "domain",
        "task_domain",
        "benchmark_domain",
        "track_domain",
    }
)


def public_task_github_meta(
    github_meta: Mapping[str, str] | None,
) -> dict[str, str] | None:
    """Keep public dataset taxonomy while removing repository and author data."""
    if not github_meta:
        return None
    projected = {
        key: value
        for key, value in github_meta.items()
        if key in PUBLIC_TASK_GITHUB_META_KEYS
    }
    return projected or None
