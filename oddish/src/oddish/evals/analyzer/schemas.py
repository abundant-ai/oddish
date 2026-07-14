from __future__ import annotations

from dataclasses import dataclass, field

from oddish.evals.primitives import SubAnalysis, TrajectoryBundle


@dataclass
class AnalyzerEvalConfig:
    analysis_model: str = "claude-haiku-4-5"
    map_concurrency: int = 16
    temperature: float = 0.0
    taxonomy_version: str = "v1"
    token_budget: int = 6000
    prompt_version: str = "v1"


@dataclass
class AnalyzerEvalInputs:
    bundles: list[TrajectoryBundle]
    subanalyses: list[SubAnalysis]


@dataclass
class Finding:
    trial_id: str
    bucket: str  # "bad" | "good"
    subcategory: str  # "1a"|"1b"|"3a"|"3b"|"3c"|"emergent:<label>"
    evidence_quote: str
    step_indices: list[int]
    root_cause: str
    headroom_signal: str
    trajectory_link: str


@dataclass
class AnalyzerEvalOutput:
    sections: dict[str, str]  # keys: bad, good, capabilities, headroom
    findings: list[Finding] = field(default_factory=list)
    counts: dict[str, int] = field(default_factory=dict)  # trials, bad, good
    breakdown: dict[str, int] = field(default_factory=dict)
    # The reduce-stage prompt that produced ``sections``, kept for debugging and
    # reproducibility. None on the zero-work path (no failures → no reduce ran).
    reduce_prompt: str | None = None
