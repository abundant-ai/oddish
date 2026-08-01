"""Which prompt files are eval-able, and which kinds a change set touches."""

from __future__ import annotations

from dataclasses import dataclass, field

GOLDENS_ROOT = "evals/prompt-goldens"
CONFIG_FILE = f"{GOLDENS_ROOT}/config.yaml"


@dataclass(frozen=True)
class PromptKindSpec:
    """One eval-able prompt file.

    ``kind`` matches the registry ``PromptKind`` value where one exists;
    ``VERDICT`` is file-only (built in code, not registry-seeded) but its
    prompt text lives in a file all the same, so it evals the same way.
    """

    kind: str
    prompt_path: str  # repo-relative
    supported: bool
    unsupported_reason: str | None = None


PROMPT_KIND_SPECS: dict[str, PromptKindSpec] = {
    spec.kind: spec
    for spec in (
        PromptKindSpec(
            kind="QA_PRE_TRIAL",
            prompt_path="oddish/src/oddish/analyze/prompts/pre_trial_qa.txt",
            supported=True,
        ),
        PromptKindSpec(
            kind="QA_POST_TRIAL",
            prompt_path="oddish/src/oddish/analyze/classify_prompt.txt",
            supported=True,
        ),
        PromptKindSpec(
            kind="VERDICT",
            prompt_path="oddish/src/oddish/analyze/verdict_prompt.txt",
            supported=True,
        ),
        PromptKindSpec(
            kind="TRAJECTORY_SUMMARY",
            prompt_path="oddish/src/oddish/analyze/prompts/trajectory_summary.txt",
            supported=False,
            unsupported_reason=(
                "trajectory summaries have no objective grading yet; changes "
                "are flagged but not scored"
            ),
        ),
    )
}


def goldens_dir_for(kind: str) -> str:
    return f"{GOLDENS_ROOT}/{kind}"


@dataclass
class ChangeSet:
    """Kinds a list of changed repo-relative paths affects.

    ``prompt_changed`` kinds get a baseline-vs-candidate comparison;
    ``goldens_changed`` kinds (prompt untouched) get a candidate-only run so a
    new golden is exercised on the PR that adds it. A config change re-runs
    every kind that has goldens, candidate-only.
    """

    prompt_changed: set[str] = field(default_factory=set)
    goldens_changed: set[str] = field(default_factory=set)
    config_changed: bool = False

    def affected_kinds(self) -> list[str]:
        kinds = self.prompt_changed | self.goldens_changed
        if self.config_changed:
            kinds |= set(PROMPT_KIND_SPECS)
        return sorted(kinds)


def classify_changed_paths(paths: list[str]) -> ChangeSet:
    changes = ChangeSet()
    by_prompt_path = {
        spec.prompt_path: spec.kind for spec in PROMPT_KIND_SPECS.values()
    }
    for raw in paths:
        path = raw.strip().lstrip("./")
        if not path:
            continue
        if path in by_prompt_path:
            changes.prompt_changed.add(by_prompt_path[path])
        elif path == CONFIG_FILE:
            changes.config_changed = True
        elif path.startswith(f"{GOLDENS_ROOT}/"):
            parts = path.split("/")
            if len(parts) >= 3 and parts[2] in PROMPT_KIND_SPECS:
                changes.goldens_changed.add(parts[2])
    return changes
