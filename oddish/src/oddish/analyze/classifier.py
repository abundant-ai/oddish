from __future__ import annotations

import asyncio
import io
import json
import os
import shlex
import tarfile
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable
import logging
from harbor.models.trial.result import TrialResult

from oddish.config import (
    ANALYSIS_MODEL,
    settings,
    to_anthropic_api_model_id,
)
from oddish.analyze._sdk_utils import Colors

from .models import (
    ActionItem,
    BaselineValidation,
    Classification,
    TrialClassification,
    TrialClassificationModel,
)

if TYPE_CHECKING:
    from oddish.blocks.analyzer.claude_cli_client import CliConfig

logger = logging.getLogger(__name__)


VERDICT_TIMEOUT = 120.0
VERDICT_MAX_TOKENS = 4096

_CLASSIFY_PROMPT_PATH = Path(__file__).parent / "classify_prompt.txt"
_CLASSIFY_PROMPT = _CLASSIFY_PROMPT_PATH.read_text()

_VERDICT_PROMPT_PATH = Path(__file__).parent / "verdict_prompt.txt"
_VERDICT_PROMPT = _VERDICT_PROMPT_PATH.read_text()


_SNAPSHOT_ARCHIVE = "/tmp/oddish-post-trial.tar.gz"


def _assert_dirs_in_snapshot(
    extra_dirs: list[Path] | None, *roots: Path
) -> None:
    """Fail loudly on context the sandbox snapshot would not carry.

    Only ``roots`` are archived. A dir outside them still reaches claude-code as
    an ``--add-dir`` path that resolves to nothing in the sandbox, and an empty
    directory reads as "no evidence" rather than as an error.
    """
    resolved_roots = [root.resolve() for root in roots]
    for extra in extra_dirs or []:
        if not any(extra.resolve().is_relative_to(r) for r in resolved_roots):
            raise ValueError(
                f"{extra} is outside the post-trial snapshot "
                f"({', '.join(str(r) for r in resolved_roots)})"
            )


def _classification_schema_json() -> str:
    """Claude Code ``--json-schema`` payload for the classification output.

    Both classifier backends constrain generation with it: the prompt's example
    object shows only the five narrative fields, so without the schema the model
    silently drops ``action_items`` / ``exploitation`` and post-trial linkage
    comes back empty.
    """
    return json.dumps(TrialClassificationModel.model_json_schema())


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
    trajectory_components: list[dict] | None = None,
) -> tuple[Path | None, str | None, str | None, str | None]:
    """Write post-trial-linkage inputs to ``<trial_dir>/.qa_context/*.json``.

    Returns ``(qa_context_dir, pre_trial_context, file_access_context,
    trajectory_components_context)``. The dir and each context string are
    ``None`` when the corresponding input was not provided, so absent inputs
    never touch disk or the prompt.

    Each context string cites the file by absolute path. The sandbox restores
    the snapshot at those same paths, so an absolute reference resolves there
    too -- a relative ``.qa_context/...`` ref would resolve against the
    sandbox's own cwd, where nothing exists.
    """
    if pre_trial_items is None and file_access is None and trajectory_components is None:
        return None, None, None, None

    qa_context_dir = trial_dir / ".qa_context"
    qa_context_dir.mkdir(parents=True, exist_ok=True)

    pre_trial_context = None
    if pre_trial_items is not None:
        (qa_context_dir / "pre_trial.json").write_text(json.dumps(pre_trial_items, indent=2))
        pre_trial_context = (
            f"{len(pre_trial_items)} pre-trial action item(s) were identified for this "
            f"task. See {qa_context_dir}/pre_trial.json for the full list (id, file, "
            "line range, title, detail)."
        )

    file_access_context = None
    if file_access is not None:
        (qa_context_dir / "file_access.json").write_text(json.dumps(file_access, indent=2))
        file_access_context = (
            f"File-access metadata for {len(file_access)} trajectory step(s) is "
            f"available. See {qa_context_dir}/file_access.json for per-step "
            "files_read/files_written/commands."
        )

    trajectory_components_context = None
    if trajectory_components is not None:
        (qa_context_dir / "trajectory_components.json").write_text(
            json.dumps(trajectory_components, indent=2)
        )
        trajectory_components_context = (
            f"A trajectory component map ({len(trajectory_components)} labeled phase "
            f"segment(s)) is available. See {qa_context_dir}/trajectory_components.json "
            "for each segment's step_ids, phase label, summary, tool_count, and "
            "duration_ms -- use it to locate the relevant steps before reading the raw "
            "trajectory."
        )

    return (
        qa_context_dir,
        pre_trial_context,
        file_access_context,
        trajectory_components_context,
    )


def build_classify_prompt(
    *,
    result_str: str,
    task_dir: str | Path,
    trial_dir: str | Path,
    trial_agent_context: str,
    pre_trial_context: str | None = None,
    file_access_context: str | None = None,
    trajectory_components_context: str | None = None,
    template: str | None = None,
) -> str:
    """Render the classification prompt.

    Extracted so the pre-trial/file-access placeholder wiring is unit-testable
    without spawning the Claude CLI subprocess. ``template`` overrides the
    packaged prompt (cloud QA passes the latest QA_POST_TRIAL registry version).
    A registry template that predates a placeholder simply ignores its kwarg.
    """
    return (template or _CLASSIFY_PROMPT).format(
        result=result_str,
        task_dir=str(task_dir),
        trial_dir=str(trial_dir),
        trial_agent_context=trial_agent_context,
        pre_trial_context=pre_trial_context or "(none)",
        file_access_context=file_access_context or "(none)",
        trajectory_components_context=trajectory_components_context or "(none)",
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
    trajectory_components: list[dict] | None = None,
) -> TrialClassification:
    """Classify a single trial outcome."""
    classifier = TrialClassifier(model=model, verbose=verbose, timeout=timeout)
    return classifier.classify_trial_sync(
        Path(trial_dir),
        Path(task_dir),
        trial_agent=trial_agent,
        pre_trial_items=pre_trial_items,
        file_access=file_access,
        trajectory_components=trajectory_components,
    )


class TrialClassifier:
    """Classifies trial outcomes using Claude Code to identify task quality issues."""

    def __init__(
        self,
        model: str = ANALYSIS_MODEL,
        verbose: bool = False,
        timeout: int = 300,
        prompt_template: str | None = None,
        on_chunk: Callable[[str], None] | None = None,
    ):
        self._model = model
        self._verbose = verbose
        self._timeout = timeout
        # Cloud QA supplies the latest QA_POST_TRIAL registry version. Keep the
        # packaged prompt as a fallback for local/library callers without a DB.
        self._prompt_template = prompt_template or _CLASSIFY_PROMPT
        # Receives each streamed analyzer event; used for the live analysis
        # log. Only the sandbox path streams during the run.
        self._on_chunk = on_chunk
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
        trajectory_components: list[dict] | None = None,
        analyzer_block_context: dict[str, Any] | None = None,
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

        (
            qa_context_dir,
            pre_trial_context,
            file_access_context,
            trajectory_components_context,
        ) = _write_qa_context(
            trial_dir, pre_trial_items, file_access, trajectory_components
        )

        prompt = build_classify_prompt(
            template=self._prompt_template,
            result_str=result_str,
            task_dir=task_dir,
            trial_dir=trial_dir,
            trial_agent_context=_get_trial_agent_context(trial_agent),
            pre_trial_context=pre_trial_context,
            file_access_context=file_access_context,
            trajectory_components_context=trajectory_components_context,
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
                extra_dirs = [qa_context_dir] if qa_context_dir else None
                if analyzer_block_context is None:
                    structured_output = await self._run_cli_directly(
                        prompt,
                        trial_dir,
                        task_dir,
                        extra_dirs=extra_dirs,
                    )
                else:
                    structured_output = await self._run_in_analyzer_block(
                        prompt=prompt,
                        trial_dir=trial_dir,
                        task_dir=task_dir,
                        extra_dirs=extra_dirs,
                        context=analyzer_block_context,
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

    async def _run_in_analyzer_block(
        self,
        *,
        prompt: str,
        trial_dir: Path,
        task_dir: Path,
        extra_dirs: list[Path] | None,
        context: dict[str, Any],
    ) -> Any:
        """Run the filesystem-aware classifier as a persisted AnalyzerBlock."""
        from oddish.blocks.analyzer.analyzer_block import (
            AnalyzerBlock,
            AnalyzerInput,
            AnalyzerType,
            resolve_substrate,
        )
        from oddish.blocks.analyzer.analyzer_llm_client import LLMClientType
        from oddish.blocks.analyzer import analyzer_llm_client
        from oddish.blocks.analyzer.analyzer_llm_client import SandboxConfig
        from oddish.blocks.analyzer.claude_cli_client import parse_stream_json_result

        client_type = resolve_substrate(
            AnalyzerType.POST_TRIAL,
            sandbox_available=analyzer_llm_client.sandbox_client_factory_registered(),
            force_sandbox=settings.post_trial_sandbox_enabled,
        )
        use_sandbox = client_type is LLMClientType.SANDBOX
        sandbox_config = None
        cli_config = None if use_sandbox else self._cli_config(
            trial_dir, task_dir, extra_dirs
        )
        block_model = self._model
        if use_sandbox:
            _assert_dirs_in_snapshot(extra_dirs, task_dir, trial_dir)
            # Archive members are the worker's own absolute paths minus the
            # leading slash, so extracting at / restores each dir exactly where
            # the prompt already says it is. Rewriting host paths into a sandbox
            # root is what breaks silently: a context file whose path is not in
            # the substitution list points the agent at an empty tree, and it
            # answers confidently from nothing.
            archive = io.BytesIO()
            with tarfile.open(fileobj=archive, mode="w:gz") as bundle:
                for local_dir in (task_dir, trial_dir):
                    bundle.add(local_dir, arcname=str(local_dir).lstrip("/"))
            parents = " ".join(
                shlex.quote(str(d.parent))
                for d in dict.fromkeys((task_dir, trial_dir))
            )
            # The sandbox authenticates with ANTHROPIC_API_KEY, so it needs the
            # plain API id -- resolve_analysis_model_and_env does the same
            # normalization for the local subprocess. An un-normalized Bedrock
            # inference-profile id reaches claude-code as an unknown model.
            block_model = to_anthropic_api_model_id(self._model) or self._model
            sandbox_config = SandboxConfig(
                session_id="post-trial",
                files_to_upload={_SNAPSHOT_ARCHIVE: archive.getvalue()},
                setup_commands=(
                    f"mkdir -p {parents} && tar -xzf {_SNAPSHOT_ARCHIVE} -C /",
                ),
                json_schema=_classification_schema_json(),
                add_dirs=(str(task_dir), str(trial_dir)),
            )

        metadata = {
            "prompt_key": context.get("prompt_key"),
            "prompt_version": context.get("prompt_version"),
            "model": block_model,
        }
        block = AnalyzerBlock(
            analyzer_type=AnalyzerType.POST_TRIAL,
            llm_client_type=client_type,
            input=AnalyzerInput(
                input={
                    "trial_id": context.get("trial_id"),
                    "task_id": context.get("task_id"),
                }
            ),
            prompt=prompt,
            analyzer_id=context.get("trial_id"),
            task_id=context.get("task_id"),
            block_metadata=metadata,
            model=block_model,
            output_transform=parse_stream_json_result,
            sandbox_config=sandbox_config,
            cli_config=cli_config,
            on_chunk=self._on_chunk,
        )
        if use_sandbox:
            # CliConfig.timeout bounds the CLAUDE_CLI path from inside, where it
            # can also kill the subprocess. The sandbox path has no inner
            # deadline, so a wedged Daytona session would hold the QA job open
            # for as long as it stayed alive.
            output = await asyncio.wait_for(block.run(), timeout=self._timeout)
        else:
            output = await block.run()
        return output.output

    def _cli_config(
        self, trial_dir: Path, task_dir: Path, extra_dirs: list[Path] | None
    ) -> CliConfig:
        """The one place the classifier describes its filesystem to claude-code.

        Both entry points build it from here, so the block path and the local
        no-DB path cannot drift in what the agent may read or how its output is
        constrained -- which is how --json-schema ended up on only one of them.
        """
        from oddish.blocks.analyzer.claude_cli_client import CliConfig

        return CliConfig(
            cwd=trial_dir,
            add_dirs=(task_dir, *(extra_dirs or [])),
            json_schema=_classification_schema_json(),
            timeout=self._timeout,
            verbose=self._verbose,
        )

    async def _run_cli_directly(
        self,
        prompt: str,
        trial_dir: Path,
        task_dir: Path,
        *,
        extra_dirs: list[Path] | None = None,
    ) -> Any:
        """Classify without a block: local runs have no DB row to persist to."""
        from oddish.blocks.analyzer.claude_cli_client import (
            ClaudeCliClient,
            parse_stream_json_result,
        )

        client = ClaudeCliClient(
            model=self._model,
            config=self._cli_config(trial_dir, task_dir, extra_dirs),
        )
        try:
            raw = "".join([chunk async for chunk in client.stream(prompt)])
        finally:
            await client.aclose()
        return parse_stream_json_result(raw)

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
        trajectory_components: list[dict] | None = None,
    ) -> TrialClassification:
        return asyncio.run(
            self.classify_trial(
                trial_dir,
                task_dir,
                trial_agent=trial_agent,
                pre_trial_items=pre_trial_items,
                file_access=file_access,
                trajectory_components=trajectory_components,
            )
        )


def build_verdict_prompt(
    classifications: list[TrialClassification],
    baseline: BaselineValidation | None = None,
    quality_check_passed: bool = True,
    pre_trial_items: list[ActionItem] | None = None,
    pre_trial_load_failed: bool = False,
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

    if pre_trial_items:
        # The classify prompt tells trials NOT to repeat a pre-trial item in
        # their action_items, so an unexploited pre-trial hole reaches the
        # verdict only through this list. The exploited flags are already
        # aggregated from the trials (aggregate_exploited_into_pre_trial runs
        # before the verdict).
        findings = "\n".join(
            f"  - [{item.tier.value}/{item.dimension.value}] {item.title}"
            + (" (a trial exploited it)" if item.exploited else " (no trial used it)")
            for item in pre_trial_items
        )
        quality_check_summary = (
            f"{len(pre_trial_items)} finding(s) from the audit of the task source:\n"
            + findings
        )
    elif pre_trial_load_failed:
        # A load failure is not a clean audit. Rendering the pass glyph here
        # would hide an unexploited must_fix leak from the verdict's rules.
        quality_check_summary = (
            "⚠ Unknown — the audit findings are not available. "
            "Do not read this as a pass."
        )
    else:
        quality_check_summary = "✓ Passed" if quality_check_passed else "✗ Failed"

    trial_lines = []
    for i, classification in enumerate(classifications, 1):
        # Without the action items, a leak that no trial used is invisible
        # here: every trial can be GOOD_* while a must_fix hole sits in the
        # items, and the verdict's leak rule keys on exactly that.
        items = "\n".join(
            f"    - [{item.tier.value}/{item.dimension.value}] {item.title}"
            for item in classification.action_items
        )
        trial_lines.append(
            f"""Trial {i}: {classification.trial_name}
  Classification: {classification.classification.value}
  Subtype: {classification.subtype}
  Reward: {classification.reward}
  Evidence: {classification.evidence}
  Root Cause: {classification.root_cause}
  Recommendation: {classification.recommendation}
  Action items:
{items if items else "    (none)"}
"""
        )
    trial_classifications = "\n".join(trial_lines)

    return _VERDICT_PROMPT.format(
        num_trials=len(classifications),
        baseline_summary=baseline_summary,
        quality_check_summary=quality_check_summary,
        trial_classifications=trial_classifications,
    )
