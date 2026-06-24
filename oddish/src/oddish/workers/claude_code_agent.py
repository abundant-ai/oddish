"""Oddish's Claude Code agent wrapper for probe trials.

Stock Harbor installs only the claude-code CLI into the sandbox (see
``harbor.agents.installed.claude_code.ClaudeCode.install``). Probe trials also
want the *harbor* package importable inside the sandbox so the agent can
``import harbor`` while exploring the harness. We pin the install to the exact
harbor the orchestrator is running -- read from the installed package's PEP 610
``direct_url.json`` so it emits a ``git+<source>@<commit>`` requirement (harbor is
a git fork, not a PyPI release). In a blessed-variant container that is the
variant's harbor; for an explicit override the caller can pass ``(source, sha)``.

This wrapper is selected only for probe claude-code trials, via
``_apply_claude_code_probe_harbor`` in :mod:`oddish.workers.harbor_agent_config`,
which points ``AgentConfig.import_path`` here.
"""

from __future__ import annotations

import json
import logging
import shlex
from importlib.metadata import Distribution, PackageNotFoundError, version

from harbor.agents.installed.base import BaseEnvironment
from harbor.agents.installed.claude_code import ClaudeCode

from oddish.core.harbor_source import harbor_git_requirement

logger = logging.getLogger(__name__)


def _installed_harbor_git_pin() -> tuple[str, str] | None:
    """``(source, commit)`` of the orchestrator's git-installed harbor, or None.

    uv/pip record the git source + resolved commit in the package's PEP 610
    ``direct_url.json``; this reflects whatever harbor was baked into the running
    container (default or a blessed variant) without any per-trial threading.
    """
    try:
        raw = Distribution.from_name("harbor").read_text("direct_url.json")
    except PackageNotFoundError:
        return None
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return None
    url = data.get("url")
    vcs_info = data.get("vcs_info") or {}
    commit = vcs_info.get("commit_id")
    if url and commit and vcs_info.get("vcs") == "git":
        return url, commit
    return None


def _pinned_harbor_requirement(
    source: str | None = None, sha: str | None = None
) -> str | None:
    """The pip requirement that installs the run's harbor into the sandbox.

    Emits a git direct reference (``harbor @ git+<source>@<sha>``) so the sandbox
    gets the same fork commit as the run: an explicit ``(source, sha)`` wins;
    otherwise the orchestrator's own git-installed harbor (via ``direct_url``);
    falling back to ``harbor==<version>`` only if harbor is a plain release.
    """
    if source and sha:
        return harbor_git_requirement(source, sha)
    pin = _installed_harbor_git_pin()
    if pin is not None:
        return harbor_git_requirement(*pin)
    try:
        return f"harbor=={version('harbor')}"
    except PackageNotFoundError:
        logger.warning("probe: harbor not installed in orchestrator; skipping pin")
        return None


class OddishClaudeCode(ClaudeCode):
    """Claude Code, plus the harbor package installed in the sandbox."""

    async def install(self, environment: BaseEnvironment) -> None:
        # Keep the stock CLI + system-package install.
        await super().install(environment)

        requirement = _pinned_harbor_requirement()
        if requirement is None:
            return

        # Best-effort: install() runs during agent setup, so a hard failure here
        # would fail the WHOLE probe trial. The harbor package is a convenience
        # for the agent's exploration, not load-bearing for the run, so mirror
        # stage_harbor_source and swallow+log failures instead of aborting.
        command = f"pip install --user --quiet {shlex.quote(requirement)}"
        try:
            await self.exec_as_agent(environment, command=command)
        except Exception:
            logger.exception(
                "probe: failed to install %s into the sandbox; "
                "the agent will run without an importable harbor",
                requirement,
            )
