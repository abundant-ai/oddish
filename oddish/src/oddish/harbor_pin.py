"""Load the locked default Harbor git pin shipped with the oddish package."""

from __future__ import annotations

import tomllib
from functools import lru_cache
from pathlib import Path

_PIN_FILE = Path(__file__).resolve().with_name("harbor-pin.toml")


@lru_cache(maxsize=1)
def load_harbor_pin() -> dict[str, str]:
    with _PIN_FILE.open("rb") as fh:
        raw = tomllib.load(fh)
    git = raw.get("git")
    rev = raw.get("rev")
    if not isinstance(git, str) or not isinstance(rev, str):
        raise ValueError(f"invalid harbor pin in {_PIN_FILE}")
    return {"git": git, "rev": rev}
