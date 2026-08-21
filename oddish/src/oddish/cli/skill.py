"""Print or install the packaged oddish SKILL.md agent guide.

This is the usage guide *for driving the oddish CLI* — unrelated to the
probe skills library uploaded to the platform (``oddish probe skill add``).
"""

from __future__ import annotations

from importlib import resources
from importlib.resources.abc import Traversable
from pathlib import Path
from typing import Annotated, Optional

import typer
from rich.console import Console

console = Console()
error_console = Console(stderr=True)

SKILL_RESOURCE_DIR = "assets/skills/oddish"
SKILL_RESOURCE = f"{SKILL_RESOURCE_DIR}/SKILL.md"
SKILL_DIR_NAME = "oddish"

# Skill directories checked in order when --install has no explicit --dir.
# Project-local shared/Codex/Claude homes win, followed by user-level homes.
_DEFAULT_SKILL_DIRS = (
    Path(".agents/skills"),
    Path(".codex/skills"),
    Path(".claude/skills"),
    Path(".kimi-code/skills"),
    Path.home() / ".agents/skills",
    Path.home() / ".codex/skills",
    Path.home() / ".claude/skills",
    Path.home() / ".kimi-code/skills",
)


def skill_text() -> str:
    """Return the packaged SKILL.md content."""
    return resources.files("oddish").joinpath(SKILL_RESOURCE).read_text()


def _default_install_root() -> Path:
    for candidate in _DEFAULT_SKILL_DIRS:
        if candidate.is_dir():
            return candidate
    return Path.home() / ".agents/skills"


def _copy_skill_tree(source: Traversable, destination: Path) -> None:
    """Copy every packaged instruction and reference into one skill folder."""
    destination.mkdir(parents=True, exist_ok=True)
    for child in source.iterdir():
        target = destination / child.name
        if child.is_dir():
            _copy_skill_tree(child, target)
        else:
            target.write_bytes(child.read_bytes())


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
    dest = root / SKILL_DIR_NAME
    source = resources.files("oddish").joinpath(SKILL_RESOURCE_DIR)
    _copy_skill_tree(source, dest)
    console.print(f"[green]Installed[/green] {dest}")


__all__ = ["skill", "skill_text"]
