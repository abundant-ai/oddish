from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from typing import Any

from harbor.models.trial.result import TrialResult
from openai import OpenAI
from rich.console import Console

from oddish.config import ANALYSIS_MODEL, VERDICT_MODEL
from oddish.analyze._sdk_utils import Colors, print_process_stream

from .models import (
    BaselineResult,
    BaselineValidation,
    Classification,
    TaskVerdict,
    TaskVerdictModel,
    TrialClassification,
    TrialClassificationModel,
)


VERDICT_TIMEOUT = 120.0
VERDICT_MAX_TOKENS = 4096

_CLASSIFY_PROMPT_PATH = Path(__file__).parent / "classify_prompt.txt"
_CLASSIFY_PROMPT = _CLASSIFY_PROMPT_PATH.read_text()

_VERDICT_PROMPT_PATH = Path(__file__).parent / "verdict_prompt.txt"
_VERDICT_PROMPT = _VERDICT_PROMPT_PATH.read_text()


def classify_trial(
    trial_dir: str | Path,
    task_dir: str | Path,
    *,
    model: str = ANALYSIS_MODEL,
    verbose: bool = False,
    timeout: int = 300,
) -> TrialClassification:
    """Classify a single trial outcome."""
    classifier = TrialClassifier(model=model, verbose=verbose, timeout=timeout)
    return classifier.classify_trial_sync(Path(trial_dir), Path(task_dir))


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
        else:
            result_str = f"unknown (reward={reward})"

        prompt = _CLASSIFY_PROMPT.format(
            result=result_str,
            task_dir=str(task_dir),
            trial_dir=str(trial_dir),
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
                    prompt, trial_dir, task_dir
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

    async def _run_claude_cli(
        self,
        prompt: str,
        trial_dir: Path,
        task_dir: Path,
    ) -> Any:
        """Run Claude Code in print mode and return structured output."""
        schema = json.dumps(TrialClassificationModel.model_json_schema())
        claude_bin = os.getenv("CC_LOGGER_REAL_CLAUDE") or "claude"
        command = [
            claude_bin,
            "-p",
            prompt,
            "--model",
            self._model,
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

        if self._verbose:
            print(
                f"{Colors.CYAN}[Classifier] Claude CLI model={self._model} cwd={trial_dir}{Colors.RESET}",
                flush=True,
            )

        process = await asyncio.create_subprocess_exec(
            *command,
            cwd=str(trial_dir),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=os.environ.copy(),
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
    ) -> TrialClassification:
        return asyncio.run(self.classify_trial(trial_dir, task_dir))

    async def classify_trials(
        self,
        trial_dirs: list[Path],
        task_dir: Path,
        console: "Console | None" = None,
    ) -> list[TrialClassification]:
        """Classify multiple trials sequentially."""
        if console:
            console.print(
                f"  Classifying {len(trial_dirs)} trial(s) with Claude Code..."
            )

        classifications = []
        for i, trial_dir in enumerate(trial_dirs):
            if console:
                console.print(f"    [{i + 1}/{len(trial_dirs)}] {trial_dir.name}...")

            try:
                classification = await self.classify_trial(trial_dir, task_dir)
                classifications.append(classification)
            except Exception as e:
                classifications.append(
                    TrialClassification(
                        trial_name=trial_dir.name,
                        classification=Classification.HARNESS_ERROR,
                        subtype="Classification Error",
                        evidence=str(e),
                        root_cause="Exception during classification",
                        recommendation="Review trial manually",
                        reward=None,
                    )
                )

        return classifications

    def classify_trials_sync(
        self,
        trial_dirs: list[Path],
        task_dir: Path,
        console: "Console | None" = None,
    ) -> list[TrialClassification]:
        return asyncio.run(self.classify_trials(trial_dirs, task_dir, console))


def _compute_task_verdict_openai(
    classifications: list[TrialClassification],
    baseline: BaselineValidation | None = None,
    quality_check_passed: bool = True,
    model: str = VERDICT_MODEL,
    console: "Console | None" = None,
    verbose: bool = False,
    api_key: str | None = None,
    timeout: float | None = None,
) -> TaskVerdict:
    """Compute task verdict using OpenAI to synthesize trial analyses."""
    if not classifications:
        return TaskVerdict(
            is_good=False,
            confidence="low",
            primary_issue="No trials to analyze",
            recommendations=["Run agent trials first"],
        )

    if not (api_key or os.getenv("OPENAI_API_KEY")):
        raise RuntimeError("OPENAI_API_KEY not set for verdict synthesis")

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

    prompt = _VERDICT_PROMPT.format(
        num_trials=len(classifications),
        baseline_summary=baseline_summary,
        quality_check_summary=quality_check_summary,
        trial_classifications=trial_classifications,
    )

    if console:
        console.print("  [dim]Synthesizing verdict with OpenAI...[/dim]")

    if verbose:
        print(
            f"\n{Colors.YELLOW}[Verdict] Synthesizing task verdict with {model}...{Colors.RESET}",
            flush=True,
        )

    client = OpenAI(
        api_key=api_key or os.getenv("OPENAI_API_KEY"),
        timeout=timeout or VERDICT_TIMEOUT,
    )

    try:
        completion = client.beta.chat.completions.parse(
            model=model,
            messages=[{"role": "user", "content": prompt}],
            response_format=TaskVerdictModel,
            max_completion_tokens=VERDICT_MAX_TOKENS,
        )

        verdict_model = completion.choices[0].message.parsed
        if verdict_model is None:
            raise RuntimeError("OpenAI returned no parsed result for verdict synthesis")

        if verbose:
            print(
                f"{Colors.GREEN}[Verdict] Verdict synthesis complete{Colors.RESET}\n",
                flush=True,
            )

    except Exception as exc:
        exc_type = type(exc).__name__
        if verbose:
            print(
                f"{Colors.RED}[Verdict] Failed ({exc_type}): {exc}{Colors.RESET}\n",
                flush=True,
            )
        raise RuntimeError(f"Verdict synthesis failed: {exc}") from exc

    task_problem_count = sum(1 for c in classifications if c.is_task_problem)
    agent_problem_count = sum(
        1 for c in classifications if c.classification == Classification.GOOD_FAILURE
    )
    success_count = sum(
        1
        for c in classifications
        if c.classification in (Classification.GOOD_SUCCESS, Classification.BAD_SUCCESS)
    )
    harness_error_count = sum(
        1 for c in classifications if c.classification == Classification.HARNESS_ERROR
    )

    return TaskVerdict(
        is_good=verdict_model.is_good,
        confidence=verdict_model.confidence,
        primary_issue=verdict_model.primary_issue,
        recommendations=verdict_model.recommendations,
        task_problem_count=task_problem_count,
        agent_problem_count=agent_problem_count,
        success_count=success_count,
        harness_error_count=harness_error_count,
        classifications=classifications,
        baseline=baseline,
    )


def compute_task_verdict(
    classifications: list[TrialClassification],
    baseline: BaselineValidation | None = None,
    quality_check_passed: bool = True,
    model: str = VERDICT_MODEL,
    console: "Console | None" = None,
    verbose: bool = False,
    api_key: str | None = None,
    timeout: float | None = None,
) -> TaskVerdict:
    """Compute overall task verdict from trial classifications."""
    return _compute_task_verdict_openai(
        classifications,
        baseline,
        quality_check_passed,
        model,
        console,
        verbose,
        api_key,
        timeout,
    )


def classify_baseline_result(
    agent: str,
    reward: float | None,
    error: str | None = None,
) -> BaselineResult:
    """Create a BaselineResult from agent run outcome."""
    passed = reward == 1.0 if reward is not None else False
    return BaselineResult(
        agent=agent,  # type: ignore[arg-type]
        passed=passed,
        reward=reward,
        error=error,
    )
