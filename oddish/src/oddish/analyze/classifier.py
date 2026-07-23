from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from typing import Any
import logging
from harbor.models.trial.result import TrialResult

from oddish.config import (
    ANALYSIS_MODEL,
    BEDROCK_ENV_VARS,
    looks_like_bedrock_model_id,
    settings,
    to_anthropic_api_model_id,
)
from oddish.analyze._sdk_utils import Colors, print_process_stream
from oddish.analyze.analysis_cost import AnalysisUsage, parse_cli_usage

from .models import (
    BaselineValidation,
    Classification,
    TrialClassification,
    TrialClassificationModel,
)

logger = logging.getLogger(__name__)


VERDICT_TIMEOUT = 120.0
VERDICT_MAX_TOKENS = 4096

_CLASSIFY_PROMPT_PATH = Path(__file__).parent / "classify_prompt.txt"
_CLASSIFY_PROMPT = _CLASSIFY_PROMPT_PATH.read_text()

_VERDICT_PROMPT_PATH = Path(__file__).parent / "verdict_prompt.txt"
_VERDICT_PROMPT = _VERDICT_PROMPT_PATH.read_text()


def _resolve_analysis_model_and_env(
    model: str, base_env: dict[str, str]
) -> tuple[str, dict[str, str]]:
    """Pick the ``--model`` id and env for the analysis Claude CLI subprocess.

    The Modal image bakes the Bedrock env vars so Claude Code defaults to
    Bedrock, and the CLI picks its route from the environment (not ``--model``).
    Two cases route this analysis call to the direct Anthropic API instead:

    * a plain (non-Bedrock) analysis model id, or
    * the force-direct incident toggle (``settings.claude_code_force_direct_api``,
      default on; mirrors ``harbor_runner._claude_code_forces_direct_api``) when
      an ``ANTHROPIC_API_KEY`` is present -- the workers' Bedrock credentials
      can't run inference (400 "Operation not allowed"), so every Bedrock
      analysis call fails until that flag is flipped off.

    In both direct-API cases, normalize any Bedrock inference-profile id back to
    its plain API id and strip the Bedrock signals so the CLI authenticates with
    ``ANTHROPIC_API_KEY``. Otherwise keep the Bedrock id and env untouched.
    """
    env = dict(base_env)
    has_api_key = bool(env.get("ANTHROPIC_API_KEY", "").strip())
    force_direct = has_api_key and settings.claude_code_force_direct_api
    if looks_like_bedrock_model_id(model) and not force_direct:
        return model, env
    for name in BEDROCK_ENV_VARS:
        env.pop(name, None)
    return (to_anthropic_api_model_id(model) or model), env


_ORACLE_TRIAL_AGENT_CONTEXT = """
## Oracle Trial Context

This trial used the `oracle` agent. It is NOT a normal autonomous agent run.
The oracle is intentionally allowed to use the reference solution from
`solution/solve.sh` or `solution/fix.patch` to validate that the intended
solution passes the verifier. This context overrides the generic normal-agent
visibility guidance below.

For this oracle trial:
- Do NOT treat reading or applying `solution/` as cheating, oracle copying, test inspection, or suspicious agent behavior.
- A passing oracle usually means the reference solution validates correctly; classify it as GOOD_SUCCESS unless you find a real harness/test defect.
- A failing oracle is evidence that the reference solution, verifier, or task packaging is broken. Classify it as BAD_FAILURE when the failure is caused by the task artifacts, or HARNESS_ERROR only for infrastructure/runtime problems.
- Do NOT classify an oracle failure as GOOD_FAILURE due to normal agent limitations, exploration mistakes, or reasoning errors.
""".strip()

_NOP_TRIAL_AGENT_CONTEXT = """
## NoOp Trial Context

This trial used the `nop`/NoOp agent. It is a baseline validation run, not a
normal task-solving agent. The NoOp agent is expected to make no meaningful fix.

For this NoOp trial:
- A failing NoOp is normally GOOD_FAILURE because the task is not pre-solved.
- A passing NoOp is suspicious and should usually be BAD_SUCCESS or HARNESS_ERROR, depending on whether tests are too permissive or the harness malfunctioned.
- Do NOT judge NoOp behavior as though it attempted and failed to solve the task.
""".strip()


def _get_trial_agent_context(trial_agent: str | None) -> str:
    normalized_agent = (trial_agent or "").strip().lower()
    if normalized_agent == "oracle":
        return f"\n{_ORACLE_TRIAL_AGENT_CONTEXT}\n"
    if normalized_agent in {"nop", "noop", "no-op"}:
        return f"\n{_NOP_TRIAL_AGENT_CONTEXT}\n"
    return ""


def _write_qa_context(
    trial_dir: Path,
    pre_trial_items: list[dict] | None,
    file_access: list[dict] | None,
) -> tuple[Path | None, str | None, str | None]:
    """Write post-trial-linkage inputs to ``<trial_dir>/.qa_context/*.json``.

    Returns ``(qa_context_dir, pre_trial_context, file_access_context)``. The
    dir and each context string are ``None`` when the corresponding input was
    not provided, so absent inputs never touch disk or the prompt.
    """
    if pre_trial_items is None and file_access is None:
        return None, None, None

    qa_context_dir = trial_dir / ".qa_context"
    qa_context_dir.mkdir(parents=True, exist_ok=True)

    pre_trial_context = None
    if pre_trial_items is not None:
        (qa_context_dir / "pre_trial.json").write_text(json.dumps(pre_trial_items, indent=2))
        pre_trial_context = (
            f"{len(pre_trial_items)} pre-trial action item(s) were identified for this "
            "task. See .qa_context/pre_trial.json for the full list (id, file, line "
            "range, title, detail)."
        )

    file_access_context = None
    if file_access is not None:
        (qa_context_dir / "file_access.json").write_text(json.dumps(file_access, indent=2))
        file_access_context = (
            f"File-access metadata for {len(file_access)} trajectory step(s) is "
            "available. See .qa_context/file_access.json for per-step "
            "files_read/files_written/commands."
        )

    return qa_context_dir, pre_trial_context, file_access_context


def build_classify_prompt(
    *,
    result_str: str,
    task_dir: str | Path,
    trial_dir: str | Path,
    trial_agent_context: str,
    pre_trial_context: str | None = None,
    file_access_context: str | None = None,
) -> str:
    """Render the classification prompt.

    Extracted so the pre-trial/file-access placeholder wiring is unit-testable
    without spawning the Claude CLI subprocess.
    """
    return _CLASSIFY_PROMPT.format(
        result=result_str,
        task_dir=str(task_dir),
        trial_dir=str(trial_dir),
        trial_agent_context=trial_agent_context,
        pre_trial_context=pre_trial_context or "(none)",
        file_access_context=file_access_context or "(none)",
    )


def classify_trial(
    trial_dir: str | Path,
    task_dir: str | Path,
    *,
    trial_agent: str | None = None,
    model: str = ANALYSIS_MODEL,
    verbose: bool = False,
    timeout: int = 300,
    pre_trial_items: list[dict] | None = None,
    file_access: list[dict] | None = None,
) -> TrialClassification:
    """Classify a single trial outcome."""
    classifier = TrialClassifier(model=model, verbose=verbose, timeout=timeout)
    return classifier.classify_trial_sync(
        Path(trial_dir),
        Path(task_dir),
        trial_agent=trial_agent,
        pre_trial_items=pre_trial_items,
        file_access=file_access,
    )


class TrialClassifier:
    """Classifies trial outcomes using Claude Code to identify task quality issues."""

    def __init__(
        self,
        model: str = ANALYSIS_MODEL,
        verbose: bool = False,
        timeout: int = 300,
    ):
        self._model = model
        self._verbose = verbose
        self._timeout = timeout
        # Usage/cost of the most recent successful CLI classification, or None.
        self.last_usage: AnalysisUsage | None = None
        self._setup_authentication()

    def _setup_authentication(self) -> None:
        """Prefer Claude OAuth when both auth modes are configured."""
        has_oauth = bool(os.getenv("CLAUDE_CODE_OAUTH_TOKEN"))
        has_api_key = bool(os.getenv("ANTHROPIC_API_KEY"))

        if has_oauth:
            if "ANTHROPIC_API_KEY" in os.environ:
                os.environ.pop("ANTHROPIC_API_KEY")
        elif has_api_key:
            if "CLAUDE_CODE_OAUTH_TOKEN" in os.environ:
                os.environ.pop("CLAUDE_CODE_OAUTH_TOKEN")

    async def classify_trial(
        self,
        trial_dir: Path,
        task_dir: Path,
        *,
        trial_agent: str | None = None,
        pre_trial_items: list[dict] | None = None,
        file_access: list[dict] | None = None,
    ) -> TrialClassification:
        """Classify a single trial outcome using Claude Code CLI."""
        result_path = trial_dir / "result.json"

        if result_path.exists():
            try:
                root_data = json.loads(result_path.read_text())
                if "n_total_trials" in root_data or "stats" in root_data:
                    for subdir in trial_dir.iterdir():
                        if subdir.is_dir() and subdir.name.startswith("task-"):
                            nested_result = subdir / "result.json"
                            if nested_result.exists():
                                result_path = nested_result
                                break
            except Exception:
                pass

        if not result_path.exists():
            return TrialClassification(
                trial_name=trial_dir.name,
                classification=Classification.HARNESS_ERROR,
                subtype="Missing Result",
                evidence="result.json not found in trial directory",
                root_cause="Trial did not complete - no result.json file",
                recommendation="Check Harbor logs for infrastructure issues",
                reward=None,
            )

        reward = None
        result_json_raw = None
        try:
            result_json_raw = json.loads(result_path.read_text())

            try:
                result = TrialResult.model_validate(result_json_raw)
                if result.verifier_result and result.verifier_result.rewards:
                    reward = result.verifier_result.rewards.get("reward")
            except Exception:
                if isinstance(result_json_raw, dict):
                    vr = result_json_raw.get("verifier_result", {})
                    if isinstance(vr, dict):
                        rewards = vr.get("rewards", {})
                        if isinstance(rewards, dict):
                            reward = rewards.get("reward")
                    if reward is None:
                        reward = result_json_raw.get("reward")
        except Exception as e:
            return TrialClassification(
                trial_name=trial_dir.name,
                classification=Classification.HARNESS_ERROR,
                subtype="Invalid Result",
                evidence=f"Could not parse result.json as JSON: {e}",
                root_cause="Trial result file is corrupted or malformed",
                recommendation="Check Harbor logs for what went wrong",
                reward=None,
            )

        if reward == 1.0:
            result_str = "pass"
        elif reward == 0.0:
            result_str = "fail"
        elif reward is not None:
            result_str = f"partial (reward={reward})"
        else:
            result_str = "unknown"

        qa_context_dir, pre_trial_context, file_access_context = _write_qa_context(
            trial_dir, pre_trial_items, file_access
        )

        prompt = build_classify_prompt(
            result_str=result_str,
            task_dir=task_dir,
            trial_dir=trial_dir,
            trial_agent_context=_get_trial_agent_context(trial_agent),
            pre_trial_context=pre_trial_context,
            file_access_context=file_access_context,
        )

        try:
            if self._verbose:
                print(
                    f"{Colors.YELLOW}[Classifier] Running Claude Code classification (timeout: {self._timeout}s)...{Colors.RESET}",
                    flush=True,
                )
                print(
                    f"{Colors.YELLOW}[Classifier] Trial: {trial_dir.name}{Colors.RESET}",
                    flush=True,
                )
                print(
                    f"{Colors.YELLOW}[Classifier] Task: {task_dir.name}{Colors.RESET}",
                    flush=True,
                )
                print("-" * 60, flush=True)

            try:
                structured_output = await self._run_claude_cli(
                    prompt,
                    trial_dir,
                    task_dir,
                    extra_dirs=[qa_context_dir] if qa_context_dir else None,
                )
            except TimeoutError:
                if self._verbose:
                    print(
                        f"{Colors.RED}[Classifier] Timed out after {self._timeout}s{Colors.RESET}",
                        flush=True,
                    )
                return TrialClassification(
                    trial_name=trial_dir.name,
                    classification=Classification.HARNESS_ERROR,
                    subtype="Timeout",
                    evidence=f"Classification timed out after {self._timeout} seconds",
                    root_cause="Claude Code classification exceeded time limit",
                    recommendation="Review trial manually or increase timeout",
                    reward=reward,
                )

            if structured_output is None:
                raise RuntimeError("Claude CLI returned no structured output")

            if self._verbose:
                print("-" * 60, flush=True)
                print(
                    f"{Colors.GREEN}[Classifier] Classification complete for {trial_dir.name}{Colors.RESET}",
                    flush=True,
                )

            return self._parse_trial_classification_structured(
                structured_output, trial_dir.name, reward
            )

        except Exception as e:
            return TrialClassification(
                trial_name=trial_dir.name,
                classification=Classification.HARNESS_ERROR,
                subtype="Classification Failed",
                evidence=f"Claude Code classification failed: {e}",
                root_cause="Could not analyze trial with Claude Code",
                recommendation="Review trial manually or check authentication",
                reward=reward,
            )

    def _stash_usage(self, payload: dict, model_id: str | None) -> None:
        self.last_usage = parse_cli_usage(payload, model_id)

    async def _run_claude_cli(
        self,
        prompt: str,
        trial_dir: Path,
        task_dir: Path,
        *,
        extra_dirs: list[Path] | None = None,
    ) -> Any:
        """Run Claude Code in print mode and return structured output."""
        self.last_usage = None
        schema = json.dumps(TrialClassificationModel.model_json_schema())
        claude_bin = os.getenv("CC_LOGGER_REAL_CLAUDE") or "claude"
        logger.info(f"choosing model: {self._model}")
        model_id, env = _resolve_analysis_model_and_env(self._model, dict(os.environ))
        logger.info(f"resolved model_id: {model_id}")
        command = [
            claude_bin,
            "-p",
            prompt,
            "--model",
            model_id,
            "--output-format",
            "json",
            "--json-schema",
            schema,
            "--tools",
            "Read,Glob",
            "--allowedTools",
            "Read",
            "Glob",
            "--permission-mode",
            "bypassPermissions",
            "--dangerously-skip-permissions",
            "--add-dir",
            str(task_dir),
        ]
        for extra_dir in extra_dirs or []:
            command += ["--add-dir", str(extra_dir)]

        if self._verbose:
            print(
                f"{Colors.CYAN}[Classifier] Claude CLI model={model_id} cwd={trial_dir}{Colors.RESET}",
                flush=True,
            )

        process = await asyncio.create_subprocess_exec(
            *command,
            cwd=str(trial_dir),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
        )

        try:
            stdout, stderr = await asyncio.wait_for(
                process.communicate(),
                timeout=self._timeout,
            )
        except asyncio.TimeoutError:
            process.kill()
            await process.communicate()
            raise TimeoutError from None

        stdout_text = stdout.decode("utf-8", errors="replace")
        stderr_text = stderr.decode("utf-8", errors="replace")

        if self._verbose:
            print_process_stream("Claude stderr", stderr_text, Colors.MAGENTA)

        if process.returncode != 0:
            error_text = (
                stderr_text.strip() or stdout_text.strip() or "Unknown Claude CLI error"
            )
            raise RuntimeError(
                f"Claude CLI exited with code {process.returncode}: {error_text}"
            )

        try:
            payload = json.loads(stdout_text)
        except json.JSONDecodeError as exc:
            if self._verbose:
                print_process_stream("Claude stdout", stdout_text, Colors.BLUE)
            raise RuntimeError(f"Claude CLI returned invalid JSON: {exc}") from exc

        self._stash_usage(payload, model_id)

        structured_output = payload.get("structured_output")
        if structured_output is not None:
            return structured_output

        result = payload.get("result")
        if isinstance(result, dict):
            return result
        if isinstance(result, str):
            try:
                return json.loads(result)
            except json.JSONDecodeError:
                pass

        raise RuntimeError("Claude CLI JSON response did not contain structured_output")

    def _parse_trial_classification_structured(
        self,
        structured_output: Any,
        trial_name: str,
        reward: float | None,
    ) -> TrialClassification:
        """Parse and validate structured classification output."""
        try:
            data: Any = structured_output

            if isinstance(data, dict):
                if "structured_output" in data and isinstance(
                    data["structured_output"], dict
                ):
                    data = data["structured_output"]
                if "result" in data and isinstance(data["result"], dict):
                    data = data["result"]

            model = TrialClassificationModel.model_validate(data)
            classification = TrialClassification.from_model(
                trial_name=trial_name, model=model, reward=reward
            )

            if reward == 1.0 and not classification.classification.is_success:
                classification.classification = Classification.BAD_SUCCESS
                classification.subtype = "Inconsistent Output"
                classification.evidence = (
                    f"Claude returned {model.classification} but verified result was pass (reward=1.0). "
                    + classification.evidence
                ).strip()
            if reward == 0.0 and classification.classification.is_success:
                classification.classification = Classification.HARNESS_ERROR
                classification.subtype = "Inconsistent Output"
                classification.evidence = (
                    f"Claude returned {model.classification} but verified result was fail (reward=0.0). "
                    + classification.evidence
                ).strip()

            return classification
        except Exception as e:
            return TrialClassification(
                trial_name=trial_name,
                classification=Classification.HARNESS_ERROR,
                subtype="Parse Error",
                evidence=f"Could not parse structured output: {e}",
                root_cause="Claude's structured output did not match expected schema",
                recommendation="Review trial manually",
                reward=reward,
            )

    def classify_trial_sync(
        self,
        trial_dir: Path,
        task_dir: Path,
        *,
        trial_agent: str | None = None,
        pre_trial_items: list[dict] | None = None,
        file_access: list[dict] | None = None,
    ) -> TrialClassification:
        return asyncio.run(
            self.classify_trial(
                trial_dir,
                task_dir,
                trial_agent=trial_agent,
                pre_trial_items=pre_trial_items,
                file_access=file_access,
            )
        )


def build_verdict_prompt(
    classifications: list[TrialClassification],
    baseline: BaselineValidation | None = None,
    quality_check_passed: bool = True,
) -> str:
    """Render the verdict-synthesis prompt, sent by ``VerdictBlock``."""
    if baseline:
        if baseline.is_valid:
            baseline_summary = (
                "✓ Passed (nop failed as expected, oracle passed as expected)"
            )
        else:
            baseline_summary = "✗ FAILED:\n" + "\n".join(
                f"  - {issue}" for issue in baseline.issues
            )
    else:
        baseline_summary = "Not run"

    quality_check_summary = "✓ Passed" if quality_check_passed else "✗ Failed"

    trial_lines = []
    for i, classification in enumerate(classifications, 1):
        trial_lines.append(
            f"""Trial {i}: {classification.trial_name}
  Classification: {classification.classification.value}
  Subtype: {classification.subtype}
  Reward: {classification.reward}
  Evidence: {classification.evidence}
  Root Cause: {classification.root_cause}
  Recommendation: {classification.recommendation}
"""
        )
    trial_classifications = "\n".join(trial_lines)

    return _VERDICT_PROMPT.format(
        num_trials=len(classifications),
        baseline_summary=baseline_summary,
        quality_check_summary=quality_check_summary,
        trial_classifications=trial_classifications,
    )
