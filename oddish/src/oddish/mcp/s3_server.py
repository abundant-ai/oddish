"""HTTP FastMCP server giving a task-scope probe read access to its whole
experiment's S3 artifacts. Scope is per-request, set from a signed URL token by
the ASGI layer (see oddish.mcp.s3_app). Tools are read-only and refuse any key
outside the experiment.
"""
from __future__ import annotations

import re
from contextvars import ContextVar

from mcp.server.fastmcp import FastMCP

from oddish.core.experiment_s3 import list_scoped, read_scoped

mcp = FastMCP("oddish-s3")

# Set per request by the ASGI middleware after token verification.
_current_experiment: ContextVar[str] = ContextVar("_current_experiment")

_MAX_READ = 200_000  # chars
_MAX_GREP_FILES = 200


def _experiment() -> str:
    try:
        return _current_experiment.get()
    except LookupError as exc:  # no scope => refuse
        raise PermissionError("no experiment scope on this request") from exc


async def _grep_impl(pattern: str, *, prefix: str, max_matches: int) -> list[dict]:
    exp = _experiment()
    rx = re.compile(pattern)
    keys = (await list_scoped(exp, prefix))[:_MAX_GREP_FILES]
    hits: list[dict] = []
    for key in keys:
        if len(hits) >= max_matches:
            break
        text = await read_scoped(exp, key, 0, _MAX_READ)
        for i, line in enumerate(text.splitlines(), start=1):
            if rx.search(line):
                hits.append({"key": key, "line_no": i, "line": line})
                if len(hits) >= max_matches:
                    break
    return hits


@mcp.tool()
async def s3_list(prefix: str = "") -> list[str]:
    """List artifact keys in this experiment (optionally under `prefix`)."""
    return await list_scoped(_experiment(), prefix)


@mcp.tool()
async def s3_grep(pattern: str, prefix: str = "", max_matches: int = 100) -> list[dict]:
    """Search file contents across the experiment. Returns {key, line_no, line}."""
    return await _grep_impl(pattern, prefix=prefix, max_matches=max_matches)


@mcp.tool()
async def s3_read(key: str, offset: int = 0, length: int = _MAX_READ) -> str:
    """Read a file's contents (range-capped) — refuses keys outside the experiment."""
    return await read_scoped(_experiment(), key, offset, min(length, _MAX_READ))
