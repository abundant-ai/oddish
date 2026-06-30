"""Profanity check for tag keys/values.

Backed by ``better-profanity`` in production. Tests monkey-patch
``_build_filter`` to inject a deterministic fake so they don't need the
real wordlist.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass


_LEET_MAP = str.maketrans({"0": "o", "1": "i", "3": "e", "4": "a", "5": "s", "7": "t"})
_REPEAT_RE = re.compile(r"(.)\1{2,}")


@dataclass
class ProfanityResult:
    allowed: bool
    hits: list[str]
    mode: str


def _normalize_for_profanity(s: str) -> str:
    s = unicodedata.normalize("NFKC", s or "").casefold()
    s = s.translate(_LEET_MAP)
    s = _REPEAT_RE.sub(r"\1\1", s)
    return s


def _build_filter(policy):  # pragma: no cover — replaced in tests
    from better_profanity import Profanity  # type: ignore[import-untyped]

    f = Profanity()
    f.load_censor_words(custom_words=list(policy.profanity_denylist or []))
    return f


def check_tag_text(text: str, policy) -> ProfanityResult:
    mode = getattr(policy, "profanity_mode", "ENFORCE")
    if mode == "OFF":
        return ProfanityResult(allowed=True, hits=[], mode=mode)
    normalized = _normalize_for_profanity(text)
    allowlist = {w.lower() for w in (getattr(policy, "profanity_allowlist", []) or [])}
    if any(word in normalized for word in allowlist):
        return ProfanityResult(allowed=True, hits=[], mode=mode)
    f = _build_filter(policy)
    f.add_censor_words(list(policy.profanity_denylist or []))
    hit = bool(f.contains_profanity(normalized) or f.contains_profanity(text))
    if not hit:
        return ProfanityResult(allowed=True, hits=[], mode=mode)
    hits = [f"matched policy denylist or builtin profanity in {text!r}"]
    if mode == "ENFORCE":
        return ProfanityResult(allowed=False, hits=hits, mode=mode)
    return ProfanityResult(allowed=True, hits=hits, mode=mode)
