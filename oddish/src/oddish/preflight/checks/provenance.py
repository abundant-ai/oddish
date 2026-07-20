from __future__ import annotations

import re
from pathlib import Path

from harbor.models.task.config import TaskConfig

from oddish.preflight.models import Finding, Severity

CHECK_ID = "provenance"

_MIN_REASON_CHARS = 10

# A regex cannot tell the task's own upstream repo from an unrelated pinned
# dependency, and getting that wrong in the permissive direction hands the agent
# the fix commit. So every fetch is flagged and the author annotates the
# legitimate ones. The grammar mirrors harbor-lh's `# anti-cheat-ok:` so authors
# learn one suppression form, not two.
_SUPPRESS_RE = re.compile(r"#\s*provenance-ok\s*:\s*(\S.*)$")

_FETCH_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"\bgit\s+clone\b"), "git clone"),
    (re.compile(r"\bgit\s+fetch\b"), "git fetch"),
    (re.compile(r"\bpip\s+install\b[^\n]*\bgit\+"), "pip install git+"),
    (
        re.compile(r"https?://[^\s\"']*/archive/[^\s\"']*\.(?:tar\.gz|tgz|zip)"),
        "repo archive URL",
    ),
    (re.compile(r"https?://codeload\.[^\s\"']+"), "codeload URL"),
)

_DOCKERIGNORE_GIT_ENTRIES = frozenset({".git", ".git/", "**/.git", "**/.git/", "*/.git"})


def _suppression_reason(line: str) -> str | None:
    """The reason text on this line's `# provenance-ok:` comment, if any."""
    m = _SUPPRESS_RE.search(line)
    return m.group(1).strip() if m else None


def _build_context_files(task_dir: Path) -> list[Path]:
    """Files that can run at image-build time and reach the agent.

    Harbor's Docker build context is environment/ (docker.py:240): the
    Dockerfile, plus any shell script it might invoke at build time. A fetch in
    any of these bakes into the image the agent works in.

    Deliberately excludes solution/solve.sh and tests/test.sh: Harbor runs those
    in the oracle and verify phases, which execute after (and outside) the agent
    phase, so a fetch there never reaches the agent. Flagging them would repeat
    the purpose-vs-mechanism error that removed dockerfile_leaks.
    """
    env_dir = task_dir / "environment"
    if not env_dir.is_dir():
        return []
    files: list[Path] = []
    dockerfile = env_dir / "Dockerfile"
    if dockerfile.is_file():
        files.append(dockerfile)
    files.extend(sorted(p for p in env_dir.rglob("*.sh") if p.is_file()))
    return files


def _fetch_findings(task_dir: Path) -> list[Finding]:
    findings: list[Finding] = []

    for path in _build_context_files(task_dir):
        text = path.read_text(encoding="utf-8", errors="ignore")
        for lineno, line in enumerate(text.splitlines(), start=1):
            if line.lstrip().startswith("#"):
                continue
            hit = next((label for p, label in _FETCH_PATTERNS if p.search(line)), None)
            if hit is None:
                continue

            reason = _suppression_reason(line)
            if reason is not None and len(reason) >= _MIN_REASON_CHARS:
                continue

            findings.append(
                Finding(
                    check_id=CHECK_ID,
                    severity=Severity.ERROR,
                    task_dir=task_dir,
                    path=path,
                    line=lineno,
                    message=(
                        f"{hit} fetches a repository. If this is the task's own "
                        "upstream, its history hands the agent the fix commit."
                    ),
                    fix_hint=(
                        "Vendor the source at a pinned revision with no history, "
                        "or annotate the line with `# provenance-ok: <reason>` "
                        f"(reason must be at least {_MIN_REASON_CHARS} characters)."
                    ),
                )
            )

    return findings


def _dockerignore_excludes_git(task_dir: Path) -> bool:
    dockerignore = task_dir / ".dockerignore"
    if not dockerignore.is_file():
        return False
    entries = {
        line.strip()
        for line in dockerignore.read_text(encoding="utf-8", errors="ignore").splitlines()
    }
    return bool(entries & _DOCKERIGNORE_GIT_ENTRIES)


def _git_findings(task_dir: Path) -> list[Finding]:
    # Scoped to the build context. Harbor builds the image with context set to
    # environment/ (harbor/environments/docker/docker.py:240), and Docker
    # refuses paths outside it — so only a .git under environment/ can reach the
    # agent. A .git elsewhere in the task dir provably cannot be baked in;
    # flagging it would be an ERROR on something that cannot happen.
    env_dir = task_dir / "environment"
    if not env_dir.is_dir():
        return []

    git_paths = sorted(p for p in env_dir.rglob(".git"))
    excluded = _dockerignore_excludes_git(task_dir)

    if git_paths:
        if excluded:
            return []
        return [
            Finding(
                check_id=CHECK_ID,
                severity=Severity.ERROR,
                task_dir=task_dir,
                path=p,
                message=(
                    "environment/ ships a .git directory, so it lands in the "
                    "agent's image. The agent can run `git log` and read the "
                    "fix commit straight out of it."
                ),
                fix_hint=(
                    "Delete the .git directory, or exclude it with a "
                    "`.dockerignore` containing `**/.git`."
                ),
            )
            for p in git_paths
        ]

    if not excluded:
        return [
            Finding(
                check_id=CHECK_ID,
                severity=Severity.WARN,
                task_dir=task_dir,
                message=(
                    "No .dockerignore excluding .git. Nothing leaks today, but "
                    "a future COPY of a git checkout into environment/ would go "
                    "unnoticed."
                ),
                fix_hint="Add a .dockerignore containing `**/.git`.",
            )
        ]

    return []


def check(task_dir: Path, config: TaskConfig) -> list[Finding]:
    return _fetch_findings(task_dir) + _git_findings(task_dir)
