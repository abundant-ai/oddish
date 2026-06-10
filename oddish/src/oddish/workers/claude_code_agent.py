"""Oddish's Claude Code agent wrapper for probe trials.

Stock Harbor installs only the claude-code CLI into the sandbox (see
``harbor.agents.installed.claude_code.ClaudeCode.install``). Probe trials also
want the *harbor* package importable inside the sandbox so the agent can
``import harbor`` while exploring the harness. We pin the install to the same
harbor version the orchestrator is running (``importlib.metadata.version``),
so the sandbox can never drift from the live harness.

This wrapper is selected only for probe claude-code trials, via
``_apply_claude_code_probe_harbor`` in :mod:`oddish.workers.harbor_runner`,
which points ``AgentConfig.import_path`` here.
"""

from __future__ import annotations

import logging
from importlib.metadata import PackageNotFoundError, version

from harbor.agents.installed.base import BaseEnvironment
from harbor.agents.installed.claude_code import ClaudeCode

logger = logging.getLogger(__name__)


def _pinned_harbor_requirement() -> str | None:
    """``harbor==<orchestrator version>`` so the sandbox matches the live pin."""
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

        # TODO(human): install `requirement` into the sandbox's Python so the
        # agent can `import harbor`. Use self.exec_as_agent(environment, command=...)
        # (agent user, not root). Decide the failure strategy: install() runs
        # during agent setup, so an uncaught error FAILS THE WHOLE TRIAL —
        # unlike stage_harbor_source which is best-effort. Pick hard-fail
        # (harbor guaranteed) or wrap in try/except + logger.exception to let
        # the probe run without it.
