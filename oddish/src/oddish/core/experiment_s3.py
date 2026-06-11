"""Scoping helpers for the per-experiment S3 MCP server.

Resolves an experiment to the set of S3 prefixes its trials own and enforces
that any requested key stays inside that set (no traversal, no sibling
experiments).
"""
from __future__ import annotations

import posixpath

from sqlalchemy import select

from oddish.db import TrialModel, get_session, get_storage_client


def _normalize(key: str) -> str:
    # Collapse ../ and ./ so traversal can't escape an allowed prefix.
    return posixpath.normpath(key).lstrip("/")


def is_within_scope(key: str, prefixes: list[str]) -> bool:
    norm = _normalize(key)
    return any(norm.startswith(_normalize(p)) for p in prefixes)


async def experiment_prefixes(experiment_id: str) -> list[str]:
    """All trial S3 prefixes belonging to an experiment."""
    async with get_session() as session:
        rows = await session.execute(
            select(TrialModel.id, TrialModel.task_id).where(
                TrialModel.experiment_id == experiment_id
            )
        )
        return [
            f"tasks/{task_id}/trials/{trial_id}/" for trial_id, task_id in rows.all()
        ]


async def list_scoped(experiment_id: str, prefix: str) -> list[str]:
    prefixes = await experiment_prefixes(experiment_id)
    storage = get_storage_client()
    base = _normalize(prefix) + "/" if prefix else ""
    out: list[str] = []
    for p in prefixes:
        if base and not (p.startswith(base) or base.startswith(p)):
            continue
        out.extend(await storage.list_keys(p if not base else base))
    # de-dup + keep only in-scope keys
    return sorted({k for k in out if is_within_scope(k, prefixes)})


async def read_scoped(experiment_id: str, key: str, offset: int, length: int) -> str:
    prefixes = await experiment_prefixes(experiment_id)
    if not is_within_scope(key, prefixes):
        raise PermissionError(f"key {key!r} is outside experiment {experiment_id}")
    storage = get_storage_client()
    data = await storage.download_bytes(_normalize(key))
    chunk = data[offset : offset + length]
    return chunk.decode("utf-8", errors="replace")
