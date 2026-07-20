from __future__ import annotations

import os
import re
from pathlib import Path

from harbor.models.task.config import TaskConfig

from oddish.preflight.models import Finding, Severity

CHECK_ID = "anti_cheat_soundness"

# Ported verbatim from harbor-lh ci_checks/_anti_cheat_scan.py. The tuning of
# these three patterns (and B3's deny/allow lists in particular) is what keeps
# the false-positive rate tolerable — do not adjust without re-testing against
# real task test suites.

# B1 — regex literals matching `import <word>` or `from <word>`.
B1_RE = re.compile(r"""r['"][^'"]*\\b?(?:import|from)\\s\+[^'"]*['"]""")

# B2 — shell `grep` for import/from, but only over an agent-writable tree.
B2_RE = re.compile(
    r"""grep\s+(?:-[a-zA-Z]+\s+)*['"][^'"]*(?:\\b)?(?:import|from)(?:\\s|\s)[^'"]*['"]"""
)

# B3 — bare-word regex `\b<word>\b` for a single lowercase identifier: a
# library-name scan. 5-char minimum avoids colliding with match groups.
B3_RE = re.compile(r"""r['"](\\b)?([a-z][a-z0-9_-]{4,})(\\b)?['"]""")
B3_DENY_CHARS = set("/.\\:?*+|()[]{}<>=!&;,$%@#^~\"' \t`")
B3_ALLOW_TOKENS = frozenset(
    {
        "passed", "failed", "total", "score", "reward",
        "metrics", "logs", "tests", "stdout", "stderr",
        "pytest", "result", "results", "report", "output",
        "ctrf", "verifier", "artifacts", "branch", "commit",
    }
)

SUPPRESS_RE = re.compile(r"#\s*anti-cheat-ok\s*:\s*\S[^\n]{9,}")

_B2_AGENT_TREES = ("/app", "rglob", "/workspace")

_FIX_HINT = (
    "Prefer a capability-level defense: encrypt golden assets, remove the "
    "library at verify time, run python3 -I -S, or block egress in task.toml. "
    "If the regex really is right, suppress with `# anti-cheat-ok: <reason>`."
)


def _is_test_file(path: Path) -> bool:
    if path.name == "test.sh":
        return True
    if path.suffix in {".py", ".sh"}:
        return f"{os.sep}tests{os.sep}" in str(path)
    return False


def _scan_line(line: str) -> str | None:
    """Return a description of the brittle pattern on this line, or None."""
    if SUPPRESS_RE.search(line):
        return None
    if B1_RE.search(line):
        return "regex scans agent source for `import <lib>`"
    if B2_RE.search(line) and any(n in line for n in _B2_AGENT_TREES):
        return "grep scans agent source for import/from"
    for m in B3_RE.finditer(line):
        literal = m.group(0)
        inner = literal[2:-1]
        if inner.startswith("\\b"):
            inner = inner[2:]
        if inner.endswith("\\b"):
            inner = inner[:-2]
        if not inner:
            continue
        if any(c in B3_DENY_CHARS for c in inner):
            continue
        if inner.lower() in B3_ALLOW_TOKENS:
            continue
        return f"bare-word regex r'{inner}' scans for a library name"
    return None


def check(task_dir: Path, config: TaskConfig) -> list[Finding]:
    tests_dir = task_dir / "tests"
    if not tests_dir.is_dir():
        return []

    findings: list[Finding] = []
    for path in sorted(tests_dir.rglob("*")):
        if not path.is_file() or not _is_test_file(path):
            continue
        text = path.read_text(encoding="utf-8", errors="ignore")
        for lineno, line in enumerate(text.splitlines(), start=1):
            kind = _scan_line(line)
            if kind is None:
                continue
            findings.append(
                Finding(
                    check_id=CHECK_ID,
                    severity=Severity.ERROR,
                    task_dir=task_dir,
                    path=path,
                    line=lineno,
                    message=(
                        f"Brittle anti-cheat: {kind}. Source scans false-positive "
                        "on legitimate mentions and are trivially evaded."
                    ),
                    fix_hint=_FIX_HINT,
                )
            )
    return findings
