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
    # WorkerJobKind of the job driving this run; prefixes every core log line so
    # entries are attributable to the job kind. Set by the host handler.
    job_kind: str = "ANALYZER"


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
    step_ids: list[int]
    root_cause: str
    headroom_signal: str
    trajectory_link: str
    model: str | None = None
    # Classifier facts, carried so the rollup can derive lanes host-side instead
    # of trusting the LLM-assigned bucket/subcategory above.
    classification: str | None = None
    subtype: str | None = None
    task_id: str | None = None
    task_path: str | None = None
    # LLM-assigned, good bucket only. Deliberately NOT derived host-side:
    # capabilities are finer-grained than the classifier's Subtype enum, so
    # nothing but the map agent reading the trajectory can distinguish e.g.
    # hypothesis-fixation from narrow-evidence-gathering (both are 3a today).
    # Plain text, not an FK -- a proposed slug has no row yet, and leaving it
    # unconstrained is what makes promotion reclassify history with no backfill.
    capability_slug: str | None = None


@dataclass
class CapabilityProposal:
    """An agent-authored capability, pending human review.

    Not carried on Finding: a proposal is not a per-trial fact, and storing it
    there would duplicate one proposal across every trial that cited it.
    """

    slug: str
    name: str
    description: str
    example: str = ""
    categories: list[str] = field(default_factory=list)
    trial_ids: list[str] = field(default_factory=list)
    trajectory_link: str = ""


@dataclass
class AnalyzerEvalOutput:
    sections: dict[str, str]  # keys: bad, good, capabilities, headroom
    findings: list[Finding] = field(default_factory=list)
    counts: dict[str, int] = field(default_factory=dict)  # trials, bad, good
    breakdown: dict[str, int] = field(default_factory=dict)
    # The reduce-stage prompt that produced ``sections``, kept for debugging and
    # reproducibility. None on the zero-work path (no failures → no reduce ran).
    reduce_prompt: str | None = None
    # The classifier subanalyses these findings were derived from. Carried on the
    # output so the trial-analyses export works for any eval strategy, including
    # ones that never build AnalyzerEvalInputs.
    subanalyses: list[SubAnalysis] = field(default_factory=list)
    # task_id -> raw trial.model values that RAN the task, including those that
    # passed it. Findings only record failures, so this cannot be derived from them.
    models_by_task: dict[str, list[str]] = field(default_factory=dict)
    # Agent-authored capabilities awaiting review. Ride the same path as
    # findings: the eval strategies hold no session, _store() persists.
    proposals: list[CapabilityProposal] = field(default_factory=list)
    # The taxonomy this run classified against. Without it, editing a capability
    # silently rewrites the meaning of every historical breakdown.
    taxonomy_version: str | None = None
    taxonomy_snapshot: dict | None = None
