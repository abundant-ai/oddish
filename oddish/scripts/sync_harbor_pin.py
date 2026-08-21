#!/usr/bin/env python3
"""Sync oddish/src/oddish/harbor-pin.toml into both pyproject harbor pins.

The TOML file is the single source of truth. Both oddish/pyproject.toml and
backend/pyproject.toml duplicate the pin because uv cannot inherit
[tool.uv.sources] across the backend worker image boundary.
"""

from __future__ import annotations

import argparse
import sys
import tomllib
from pathlib import Path

import tomlkit

_ODDISH_ROOT = Path(__file__).resolve().parents[1]
_REPO_ROOT = _ODDISH_ROOT.parent
_PIN_FILE = _ODDISH_ROOT / "src/oddish/harbor-pin.toml"
_TARGETS = (
    _ODDISH_ROOT / "pyproject.toml",
    _REPO_ROOT / "backend" / "pyproject.toml",
)


def _load_pin() -> dict[str, str]:
    with _PIN_FILE.open("rb") as fh:
        raw = tomllib.load(fh)
    git = raw.get("git")
    rev = raw.get("rev")
    if not isinstance(git, str) or not isinstance(rev, str):
        raise SystemExit(f"invalid harbor pin in {_PIN_FILE}")
    return {"git": git, "rev": rev}


def _sync_file(path: Path, pin: dict[str, str], *, check: bool) -> bool:
    text = path.read_text(encoding="utf-8")
    doc = tomlkit.parse(text)
    sources = doc.get("tool", {}).get("uv", {}).get("sources")
    if not isinstance(sources, dict) or "harbor" not in sources:
        raise SystemExit(f"missing [tool.uv.sources].harbor in {path}")
    harbor = sources["harbor"]
    if not isinstance(harbor, dict):
        raise SystemExit(f"invalid [tool.uv.sources].harbor in {path}")

    changed = harbor.get("git") != pin["git"] or harbor.get("rev") != pin["rev"]
    if check:
        return changed

    harbor["git"] = pin["git"]
    harbor["rev"] = pin["rev"]
    path.write_text(tomlkit.dumps(doc), encoding="utf-8")
    return changed


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check",
        action="store_true",
        help="exit 1 when any pyproject pin drifts from harbor-pin.toml",
    )
    args = parser.parse_args()

    pin = _load_pin()
    drifted: list[Path] = []
    for target in _TARGETS:
        if _sync_file(target, pin, check=args.check):
            drifted.append(target)

    if args.check:
        if drifted:
            rel = ", ".join(str(p.relative_to(_REPO_ROOT)) for p in drifted)
            print(
                "harbor pin drift: run `cd oddish && uv run python scripts/sync_harbor_pin.py` "
                f"to sync {rel} with src/oddish/harbor-pin.toml",
                file=sys.stderr,
            )
            return 1
        print("harbor pin sync: OK")
        return 0

    if drifted:
        rel = ", ".join(str(p.relative_to(_REPO_ROOT)) for p in drifted)
        print(f"updated harbor pin in {rel}")
    else:
        print("harbor pin already in sync")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
