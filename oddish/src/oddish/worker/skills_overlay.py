"""Pure helper to materialize skills into a sandbox ``.claude/skills`` dir.

Kept free of DB/S3 side effects so phase 2 can wire it into ``local_runner``
(``skills_root = work_root/.claude/skills``) and ``trial_handler`` while this
stays unit-testable. The probe sandbox is ``network_mode: none``, so skills
must be written from already-loaded data at stage time.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class SkillBundle:
    """A skill ready to write to disk: its dir name and (relative_path, content) files."""

    name: str
    files: list[tuple[str, str]] = field(default_factory=list)


def materialize_skills(skills: list[SkillBundle], skills_root: Path) -> list[Path]:
    """Write each skill under ``skills_root/<name>/<relative_path>``.

    Returns the list of files written. Raises ``ValueError`` if any file would
    escape its skill directory (defense against ``..`` in stored paths).
    """
    skills_root = Path(skills_root)
    written: list[Path] = []
    for skill in skills:
        skill_dir = (skills_root / skill.name).resolve()
        for relative_path, content in skill.files:
            target = (skill_dir / relative_path).resolve()
            if not target.is_relative_to(skill_dir):
                raise ValueError(
                    f"skill {skill.name!r}: path {relative_path!r} escapes skill dir"
                )
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content)
            written.append(target)
    return written
