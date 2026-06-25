"""Read vendored seed skill bundles from ``seeds/skillz/`` via pathlib.

Each child dir of ``skillz/`` is one bundle whose ``name``/``description``
come from its SKILL.md frontmatter; files are returned as
``(relative_path, content)`` pairs for direct insertion into skill_files.
"""

from __future__ import annotations

from pathlib import Path

import yaml


def _frontmatter_name_desc(skill_md: str) -> tuple[str, str]:
    parts = skill_md.lstrip().split("---", 2)
    meta = (yaml.safe_load(parts[1]) or {}) if len(parts) >= 3 else {}
    return str(meta.get("name", "")), str(meta.get("description", ""))


def load_seed_bundles() -> list[dict]:
    root = Path(__file__).parent / "skillz"
    bundles: list[dict] = []
    for child in sorted(root.iterdir()):
        if not child.is_dir():
            continue
        files: list[tuple[str, str]] = []
        skill_md = ""
        for f in sorted(child.rglob("*")):
            if not f.is_file():
                continue
            rel = str(f.relative_to(child))
            content = f.read_text(encoding="utf-8")
            files.append((rel, content))
            if rel == "SKILL.md":
                skill_md = content
        name, description = _frontmatter_name_desc(skill_md)
        bundles.append(
            {"name": name or child.name, "description": description, "files": files}
        )
    return bundles
