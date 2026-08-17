"""Print or install the packaged oddish SKILL.md agent guide.

This is the usage guide *for driving the oddish CLI* — unrelated to the
probe skills library uploaded to the platform (``oddish probe skill add``).
"""

from __future__ import annotations

from importlib import resources
from pathlib import Path
from typing import Annotated, Optional

import typer
from rich.console import Console

console = Console()
error_console = Console(stderr=True)

SKILL_RESOURCE = "assets/skills/oddish/SKILL.md"
SKILL_DIR_NAME = "oddish"

# Skill directories checked in order when --install has no explicit --dir.
# Project-local Claude first, then well-known user-level agent homes.
_DEFAULT_SKILL_DIRS = (
    Path(".claude/skills"),
    Path.home() / ".claude/skills",
    Path.home() / ".kimi-code/skills",
    Path.home() / ".agents/skills",
)


def skill_text() -> str:
    """Return the packaged SKILL.md content."""
    return resources.files("oddish").joinpath(SKILL_RESOURCE).read_text()


def _default_install_root() -> Path:
    for candidate in _DEFAULT_SKILL_DIRS:
        if candidate.is_dir():
            return candidate
    return _DEFAULT_SKILL_DIRS[1]


def skill(
    install: Annotated[
        bool,
        typer.Option(
            "--install",
            help="Copy the skill into an agent skills directory instead of printing",
        ),
    ] = False,
    dir: Annotated[
        Optional[Path],
        typer.Option(
            "--dir",
            help="Skills directory for --install (default: first detected agent skills dir)",
        ),
    ] = None,
    path: Annotated[
        bool,
        typer.Option("--path", help="Print the packaged SKILL.md location"),
    ] = False,
) -> None:
    """Print (or install) the SKILL.md that teaches agents to drive this CLI."""

    if path:
        resource = resources.files("oddish").joinpath(SKILL_RESOURCE)
        print(resource)
        return

    if not install:
        # Plain print (not Rich) so the markdown pipes cleanly into files.
        print(skill_text(), end="")
        return

    root = dir or _default_install_root()
    dest = root / SKILL_DIR_NAME / "SKILL.md"
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(skill_text())
    console.print(f"[green]Installed[/green] {dest}")


__all__ = ["skill", "skill_text"]
