"""Per-step file-access metadata parsed from a trial's agent JSONL, so the
post-trial classifier can match pre-trial action-item file refs structurally."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from oddish.worker.probe_analysis import _find_first

_READ_TOOLS = {"Read", "View", "Cat"}
_WRITE_TOOLS = {"Edit", "Write", "MultiEdit"}


@dataclass
class TrajectoryFileAccess:
    step_index: int
    tool: str
    files_read: list[str] = field(default_factory=list)
    files_written: list[str] = field(default_factory=list)
    commands: list[str] = field(default_factory=list)


def _iter_tool_uses(log_path: Path):
    step = 0
    for line in log_path.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except Exception:
            continue
        if event.get("type") != "assistant":
            continue
        content = (event.get("message") or {}).get("content") or []
        for block in content:
            if not isinstance(block, dict) or block.get("type") != "tool_use":
                continue
            step += 1
            yield step, block.get("name", "?"), block.get("input") or {}


def parse_trajectory_file_access(trial_dir: Path) -> list[TrajectoryFileAccess]:
    log_path = _find_first(Path(trial_dir), "claude-code.txt")
    if log_path is None:
        return []
    out: list[TrajectoryFileAccess] = []
    for step, tool, inp in _iter_tool_uses(log_path):
        access = TrajectoryFileAccess(step_index=step, tool=tool)
        if tool in _READ_TOOLS and isinstance(inp.get("file_path"), str):
            access.files_read.append(inp["file_path"])
        elif tool == "Glob" and isinstance(inp.get("pattern"), str):
            access.files_read.append(inp["pattern"])
        elif tool in _WRITE_TOOLS and isinstance(inp.get("file_path"), str):
            access.files_written.append(inp["file_path"])
        elif tool == "Bash" and isinstance(inp.get("command"), str):
            access.commands.append(inp["command"])
        out.append(access)
    return out
