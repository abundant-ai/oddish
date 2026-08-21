"""Oddish's Claude Code agent wrappers.

All Claude Code trials deliver the task over stdin so task text is absent from
the process command line. Probe trials additionally install Harbor into the
sandbox so the agent can inspect the harness.

Stock Harbor installs only the claude-code CLI into the sandbox (see
``harbor.agents.installed.claude_code.ClaudeCode.install``). Probe trials also
want the *harbor* package importable inside the sandbox so the agent can
``import harbor`` while exploring the harness. We pin the install to the exact
harbor the orchestrator is running -- read from the installed package's PEP 610
``direct_url.json`` (harbor is a git fork, not a PyPI release) and rendered as
a requirement the sandbox can actually install: probe sandbox images ship no
``git`` binary, so GitHub sources become a commit tarball rather than a
``git+`` reference. In a blessed-variant container that is the variant's
harbor; for an explicit override the caller can pass ``(source, sha)``.

The wrappers are selected by ``_apply_claude_code_oddish_wrapper`` in
:mod:`oddish.workers.harbor.agent_config`.
"""

from __future__ import annotations

import base64
import json
import logging
import shlex
from importlib.metadata import Distribution, PackageNotFoundError, version

from harbor.agents.installed.base import BaseEnvironment
from harbor.agents.installed.claude_code import ClaudeCode
from harbor.models.agent.context import AgentContext

from oddish.core.harbor_source import harbor_sandbox_requirement

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

    Emits a direct reference the sandbox can install WITHOUT a git binary
    (GitHub sources render as the commit tarball; see
    ``harbor_sandbox_requirement``) so the sandbox gets the same fork commit
    as the run: an explicit ``(source, sha)`` wins; otherwise the
    orchestrator's own git-installed harbor (via ``direct_url``); falling
    back to ``harbor==<version>`` only if harbor is a plain release.
    """
    if source and sha:
        return harbor_sandbox_requirement(source, sha)
    pin = _installed_harbor_git_pin()
    if pin is not None:
        return harbor_sandbox_requirement(*pin)
    try:
        return f"harbor=={version('harbor')}"
    except PackageNotFoundError:
        logger.warning("probe: harbor not installed in orchestrator; skipping pin")
        return None


def _pinned_oddish_requirement() -> str | None:
    """The pip requirement that installs an ``oddish`` CLI matching this
    orchestrator, so the sandbox's ``oddish pull`` speaks the same API/schema
    the server expects.

    Unlike harbor (a git fork with no PyPI release), oddish is published to
    PyPI, so pinning the exact installed version is enough -- no git
    ``direct_url`` resolution needed.
    """
    try:
        return f"oddish=={version('oddish')}"
    except PackageNotFoundError:
        logger.warning("pre-trial: oddish not installed in orchestrator; skipping pin")
        return None


class OddishClaudeCode(ClaudeCode):
    """Claude Code with task delivery kept off the process command line."""

    def _build_claude_command(self, escaped_instruction: str, extra_flags: str) -> str:
        """Pipe the prompt over stdin instead of including it in ``claude`` argv.

        Long-horizon tasks often restart services with ``pkill -f``. When the
        full task is on argv, a service name from that prompt can match and kill
        the agent itself. Encoding only protects the transport command line;
        ``base64 -d`` restores the exact UTF-8 prompt on Claude Code's stdin.
        """
        try:
            instruction_parts = shlex.split(escaped_instruction)
        except ValueError as exc:
            raise ValueError(
                "Claude Code instruction is not valid shell quoting"
            ) from exc
        if len(instruction_parts) != 1:
            raise ValueError(
                "Claude Code instruction must decode to exactly one shell argument"
            )

        encoded_instruction = base64.b64encode(
            instruction_parts[0].encode("utf-8")
        ).decode("ascii")
        return (
            'export PATH="$HOME/.local/bin:$PATH"; '
            f"printf %s {shlex.quote(encoded_instruction)} | base64 -d | "
            "claude --verbose --output-format=stream-json "
            f"{extra_flags}"
            "--print 2>&1 | tee /logs/agent/claude-code.txt"
        )


class OddishAnalysisClaudeCode(OddishClaudeCode):
    """Claude Code with bounded, same-session analysis artifact repair.

    Analysis agents do the expensive evidence gathering once. After the first
    response, this wrapper runs the same deterministic checker the Harbor
    verifier will run. A malformed artifact receives only the concrete checker
    errors and resumes the existing Claude session to edit the file in place.
    Harbor still runs its verifier after this method returns and remains the
    authority that accepts or rejects the result.
    """

    _MAX_REPAIR_ATTEMPTS = 2
    _VALIDATOR = "/probe-harness/validate-analysis-result"

    async def _validate_analysis_artifact(
        self,
        environment: BaseEnvironment,
        *,
        artifact: str,
        attempt: int,
    ) -> tuple[bool, str, bool]:
        artifact_path = f"/logs/{artifact}"
        stem, separator, suffix = artifact.rpartition(".")
        attempt_name = (
            f"{stem}.attempt-{attempt}.{suffix}"
            if separator
            else f"{artifact}.attempt-{attempt}"
        )
        attempt_dir = "/logs/agent/analysis-attempts"
        saved_path = f"{attempt_dir}/{attempt_name}"
        validation_path = f"{attempt_dir}/validation.attempt-{attempt}.txt"
        command = (
            f"mkdir -p {attempt_dir}; "
            f"if [ -f {shlex.quote(artifact_path)} ]; then "
            f"cp {shlex.quote(artifact_path)} {shlex.quote(saved_path)}; fi; "
            f"{self._VALIDATOR} {shlex.quote(artifact_path)} "
            f"> {shlex.quote(validation_path)} 2>&1; "
            "validation_status=$?; "
            f"cat {shlex.quote(validation_path)}; "
            "exit $validation_status"
        )
        result = await environment.exec(command=command, timeout_sec=60)
        output = "\n".join(
            part.strip() for part in (result.stdout, result.stderr) if part
        ).strip()
        validator_available = not (
            result.return_code in (126, 127)
            or "validate-analysis-result: not found" in output
            or "No such file or directory" in output
        )
        return result.return_code == 0, output, validator_available

    async def run(
        self,
        instruction: str,
        environment: BaseEnvironment,
        context: AgentContext,
    ) -> None:
        await super().run(instruction, environment, context)

        artifact = (self._get_env("ODDISH_ANALYSIS_ARTIFACT") or "").strip()
        if not artifact:
            self.logger.warning(
                "analysis wrapper received no ODDISH_ANALYSIS_ARTIFACT; "
                "leaving validation to Harbor"
            )
            return

        original_resume = self._resume
        try:
            for attempt in range(1, self._MAX_REPAIR_ATTEMPTS + 2):
                (
                    valid,
                    errors,
                    validator_available,
                ) = await self._validate_analysis_artifact(
                    environment,
                    artifact=artifact,
                    attempt=attempt,
                )
                if valid:
                    return
                if not validator_available:
                    self.logger.warning(
                        "analysis validator is unavailable in the sandbox; "
                        "leaving validation to Harbor"
                    )
                    return
                if attempt > self._MAX_REPAIR_ATTEMPTS:
                    self.logger.warning(
                        "analysis artifact %s remains invalid after %d repairs: %s",
                        artifact,
                        self._MAX_REPAIR_ATTEMPTS,
                        errors,
                    )
                    return

                await environment.exec(
                    command=(
                        "if [ -f /logs/agent/claude-code.txt ]; then "
                        "cp /logs/agent/claude-code.txt "
                        f"/logs/agent/claude-code.attempt-{attempt}.txt; fi"
                    ),
                    timeout_sec=30,
                )
                repair_prompt = f"""Analysis is complete. Do not repeat the analysis or fetch trial data again.

The existing file `/logs/{artifact}` failed the deterministic output validator with these errors:

{errors[:12000] or "The validator rejected the file without an error message."}

Repair `/logs/{artifact}` in place. Preserve valid evidence and conclusions. Read `/probe-harness/expected.json` for the exact machine contract. Then run:

`/probe-harness/validate-analysis-result /logs/{artifact}`

Do not finish until that command exits successfully. Output no alternative file and do not merely claim validation passed."""
                self._resume = True
                await super().run(repair_prompt, environment, context)
        finally:
            self._resume = original_resume


class OddishProbeClaudeCode(OddishClaudeCode):
    """Claude Code plus the Harbor package installed for probe trials."""

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
