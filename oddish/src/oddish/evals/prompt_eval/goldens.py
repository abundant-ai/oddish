"""Load the golden corpus: eval config plus per-kind cases.

Layout (repo root)::

    evals/prompt-goldens/
      config.yaml                  # agents, trials, timeouts
      <KIND>/cases/<case-name>/
        case.yaml                  # expected labels (+ inputs for VERDICT)
        task/                      # task source snapshot (PRE/POST trial kinds)
        trial/                     # trial artifacts snapshot (POST trial kind)
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import yaml


class GoldenLoadError(ValueError):
    """A case or config file is malformed. Raised, not skipped: a golden that
    silently drops out of the corpus inflates every future pass rate."""


@dataclass(frozen=True)
class AgentSpec:
    """One analysis agent (judge) the prompts are evaluated with. ``model`` is
    whatever the Claude CLI accepts as ``--model``."""

    name: str
    model: str


@dataclass(frozen=True)
class EvalConfig:
    agents: tuple[AgentSpec, ...]
    n_trials: int = 1
    timeout_seconds: float = 300.0
    concurrency: int = 4
    max_cases_per_kind: int = 25


DEFAULT_AGENTS = (
    AgentSpec(name="sonnet", model="claude-sonnet-5"),
    AgentSpec(name="haiku", model="claude-haiku-4-5"),
)


def load_config(goldens_root: Path) -> EvalConfig:
    config_path = goldens_root / "config.yaml"
    if not config_path.exists():
        return EvalConfig(agents=DEFAULT_AGENTS)
    data = yaml.safe_load(config_path.read_text()) or {}
    if not isinstance(data, dict):
        raise GoldenLoadError(f"{config_path} must be a mapping")
    agents: list[AgentSpec] = []
    for entry in data.get("agents") or []:
        if not isinstance(entry, dict) or "model" not in entry:
            raise GoldenLoadError(f"{config_path}: each agent needs a 'model'")
        agents.append(
            AgentSpec(
                name=str(entry.get("name") or entry["model"]), model=str(entry["model"])
            )
        )
    return EvalConfig(
        agents=tuple(agents) or DEFAULT_AGENTS,
        n_trials=int(data.get("n_trials", 1)),
        timeout_seconds=float(data.get("timeout_seconds", 300)),
        concurrency=int(data.get("concurrency", 4)),
        max_cases_per_kind=int(data.get("max_cases_per_kind", 25)),
    )


@dataclass(frozen=True)
class GoldenCase:
    kind: str
    name: str
    case_dir: Path
    expected: dict
    description: str = ""
    trial_agent: str | None = None  # agent that produced the golden trajectory
    inputs: dict = field(default_factory=dict)  # VERDICT template fields

    @property
    def task_dir(self) -> Path:
        return self.case_dir / "task"

    @property
    def trial_dir(self) -> Path:
        return self.case_dir / "trial"


_REQUIRED_DIRS = {
    "QA_PRE_TRIAL": ("task",),
    "QA_POST_TRIAL": ("task", "trial"),
    "VERDICT": (),
}

_VERDICT_INPUT_KEYS = (
    "num_trials",
    "baseline_summary",
    "quality_check_summary",
    "trial_classifications",
)


def _load_case(kind: str, case_dir: Path) -> GoldenCase:
    case_file = case_dir / "case.yaml"
    if not case_file.exists():
        raise GoldenLoadError(f"{case_dir} has no case.yaml")
    data = yaml.safe_load(case_file.read_text()) or {}
    if not isinstance(data, dict) or not isinstance(data.get("expected"), dict):
        raise GoldenLoadError(f"{case_file} needs an 'expected' mapping")
    for sub in _REQUIRED_DIRS.get(kind, ()):
        if not (case_dir / sub).is_dir():
            raise GoldenLoadError(f"{case_dir} is missing its {sub}/ directory")
    inputs = data.get("inputs") or {}
    if kind == "VERDICT":
        missing = [key for key in _VERDICT_INPUT_KEYS if key not in inputs]
        if missing:
            raise GoldenLoadError(f"{case_file} inputs missing {missing}")
    return GoldenCase(
        kind=kind,
        name=case_dir.name,
        case_dir=case_dir,
        expected=data["expected"],
        description=str(data.get("description") or ""),
        trial_agent=data.get("agent"),
        inputs=inputs,
    )


def load_cases(
    goldens_root: Path, kind: str, *, max_cases: int | None = None
) -> list[GoldenCase]:
    cases_dir = goldens_root / kind / "cases"
    if not cases_dir.is_dir():
        return []
    cases = [
        _load_case(kind, case_dir)
        for case_dir in sorted(p for p in cases_dir.iterdir() if p.is_dir())
    ]
    if max_cases is not None and len(cases) > max_cases:
        raise GoldenLoadError(
            f"{kind} has {len(cases)} cases, over the max_cases_per_kind={max_cases} "
            "cost guard; raise it in config.yaml if intended"
        )
    return cases
