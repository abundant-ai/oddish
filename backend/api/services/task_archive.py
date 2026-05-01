"""Build tar.gz archives of task source directories for the agent-sandbox-service."""
from __future__ import annotations

import io
import tarfile
import tomllib
from dataclasses import dataclass
from pathlib import Path

# Files inside the task source dir we never want to ship over the wire.
_SKIP_DIRS = {".git", "node_modules", "__pycache__", "dist", "build", "target"}
_PRIORITY_FILES = ("task.toml", "instruction.md")
_PRIORITY_DIRS = ("verifier",)
_PER_FILE_CAP = 1 * 1024 * 1024
_TOTAL_CAP = 10 * 1024 * 1024


@dataclass
class TaskArchive:
    body: bytes
    task_name: str
    verifier_command: str


def build_task_archive(task_path: Path, fallback_name: str) -> TaskArchive:
    """Returns a gzipped tar of the task source, plus extracted task name and
    verifier command.

    Mirrors the file-walk used by oddish's existing cc-chat code in
    routers/cc_chat.py: priority files always included; discretionary tail
    capped per-file (1MB) and overall (10MB)."""
    task_name = fallback_name
    verifier_command = "bash verifier/run.sh"

    toml_path = task_path / "task.toml"
    if toml_path.is_file():
        try:
            doc = tomllib.loads(toml_path.read_text())
            task_name = doc.get("name") or fallback_name
            verifier_command = doc.get("verifier_command") or verifier_command
        except Exception:
            pass

    out = io.BytesIO()
    with tarfile.open(fileobj=out, mode="w:gz") as tar:
        seen: set[Path] = set()

        def _add(p: Path) -> bool:
            if p in seen or not p.is_file():
                return False
            rel_parts = p.relative_to(task_path).parts
            if any(part in _SKIP_DIRS or part.startswith(".") for part in rel_parts):
                return False
            try:
                content = p.read_bytes()
            except OSError:
                return False
            info = tarfile.TarInfo(name="/".join(rel_parts))
            info.size = len(content)
            tar.addfile(info, io.BytesIO(content))
            seen.add(p)
            return True

        # Phase 1: priority files
        for name in _PRIORITY_FILES:
            _add(task_path / name)
        for sub in _PRIORITY_DIRS:
            sub_path = task_path / sub
            if sub_path.is_dir():
                for path in sorted(sub_path.rglob("*")):
                    _add(path)

        # Phase 2: discretionary tail
        discretionary_total = 0
        for path in sorted(task_path.rglob("*")):
            if path in seen or not path.is_file():
                continue
            try:
                size = path.stat().st_size
            except OSError:
                continue
            if size > _PER_FILE_CAP:
                continue
            if discretionary_total + size > _TOTAL_CAP:
                break
            if _add(path):
                discretionary_total += size

    return TaskArchive(
        body=out.getvalue(),
        task_name=task_name,
        verifier_command=verifier_command,
    )
