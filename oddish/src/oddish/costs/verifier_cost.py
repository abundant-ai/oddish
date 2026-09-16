"""CUA / LLM verifier spend: locate artifacts and price drafts.

Does not write ``verifier_costs`` — settlement and backfill land in later
PRs. Distinct from ``trials.cost_usd`` (solver) and ``analysis_costs`` (QA).

Covers Harbor ``type = "cua"`` (``verifier/``) and SWE-Marathon inline CUA
(``verifier/ux/``, ``cua_judge_report.json``).

Route uses the model id spelling: ``anthropic/…`` → Claude console;
``bedrock/…`` and Bedrock inference-profile ids → AWS.
"""

from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from oddish.core.llm_key_fingerprint import platform_key_hash_for_provider
from oddish.model_pricing import estimate_cost_usd

COMPONENT_LOOP = "cua_loop"
COMPONENT_JUDGE = "cua_judge"

ROUTE_ANTHROPIC = "anthropic"
ROUTE_BEDROCK = "bedrock"
ROUTE_OTHER = "other"

COST_NATIVE = "native"
COST_ESTIMATED = "estimated"
COST_BACKFILL = "backfill"

UNPRICED_MISSING_TRAJECTORY = "missing_trajectory"
UNPRICED_MISSING_USAGE = "missing_usage"

# Conservative per-criterion floor when the judge response has no usage.
_JUDGE_INPUT_TOKENS_PER_CRITERION = 2_000
_JUDGE_OUTPUT_TOKENS_PER_CRITERION = 300

_JUDGE_REPORT_NAMES = ("cua_judge_report.json",)
_TRAJECTORY_NAMES = ("trajectory.json",)
# Known CUA output roots (Harbor + SWE-M). Prefer these over rglob.
_CUA_OUTPUT_RELS = ("verifier/ux", "verifier")
UNPRICED_NO_CUA_ARTIFACTS = "no_cua_artifacts"


@dataclass(frozen=True)
class VerifierCostDraft:
    component: str
    model: str | None
    route: str
    llm_key_hash: str | None
    input_tokens: int | None
    output_tokens: int | None
    cache_read_tokens: int | None
    cache_write_tokens: int | None
    cost_usd: float | None
    cost_source: str
    unpriced_reason: str | None = None


@dataclass(frozen=True)
class CuaArtifactBundle:
    """Resolved CUA artifacts for one trial attempt."""

    verifier_dir: Path
    trajectory_path: Path | None
    judge_report_path: Path | None
    judge_report: dict[str, Any] | None
    loop_model: str | None
    judge_model: str | None


def infer_verifier_route(model: str | None) -> str:
    """Map a verifier model id to a billing recon bucket.

    ``anthropic/…`` and bare Claude ids → Anthropic (Claude console).
    Explicit ``bedrock/…`` or Bedrock inference-profile ids
    (``us.anthropic.*`` / ``global.anthropic.*``) → Bedrock.
    """
    raw = (model or "").strip().lower()
    if not raw:
        return ROUTE_OTHER
    if (
        raw.startswith("bedrock/")
        or raw.startswith("bedrock.")
        or raw.startswith("us.anthropic.")
        or raw.startswith("global.anthropic.")
    ):
        return ROUTE_BEDROCK
    if (
        raw.startswith("anthropic/")
        or raw.startswith("claude")
        or "/claude" in raw
    ):
        return ROUTE_ANTHROPIC
    return ROUTE_OTHER


def _key_hash_for_route(route: str) -> str | None:
    if route == ROUTE_ANTHROPIC:
        return platform_key_hash_for_provider("anthropic")
    if route == ROUTE_BEDROCK:
        return platform_key_hash_for_provider("bedrock")
    return None


def _as_int(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _as_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        n = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(n) or n < 0:
        return None
    return n


def _load_json(path: Path) -> dict[str, Any] | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def _metrics_from_atif(data: dict[str, Any]) -> tuple[
    int | None, int | None, int | None, int | None, float | None
]:
    """Return input, output, cache_read, cache_write, cost from an ATIF object."""
    final_metrics = data.get("final_metrics")
    input_tokens = output_tokens = cache_tokens = cache_write = cost = None
    if isinstance(final_metrics, dict):
        input_tokens = _as_int(final_metrics.get("total_prompt_tokens"))
        output_tokens = _as_int(final_metrics.get("total_completion_tokens"))
        cache_tokens = _as_int(final_metrics.get("total_cached_tokens"))
        cost = _as_float(final_metrics.get("total_cost_usd"))
        extra = final_metrics.get("extra")
        if isinstance(extra, dict):
            for key in (
                "cache_creation_input_tokens",
                "input_cache_creation",
                "cacheWriteTokens",
                "cache_write_tokens",
            ):
                if extra.get(key) is not None:
                    cache_write = _as_int(extra.get(key))
                    break
    return input_tokens, output_tokens, cache_tokens, cache_write, cost


def find_cua_artifact_dirs(job_dir: Path) -> list[Path]:
    """Directories that look like a CUA verifier output root.

    Harbor's job root holds a trial-name subdirectory; artifacts live under
    ``<job>/<trial_name>/verifier[/ux]``, not ``<job>/verifier``. Also accept
    a flattened tree (backfill / selected trial dir) at ``job_dir`` itself.
    """
    if not job_dir or not job_dir.exists():
        return []
    roots: list[Path] = [job_dir]
    try:
        roots.extend(p for p in sorted(job_dir.iterdir()) if p.is_dir())
    except OSError:
        pass
    found: list[Path] = []
    seen: set[Path] = set()
    for root in roots:
        for rel in _CUA_OUTPUT_RELS:
            directory = (root / rel).resolve()
            if directory in seen or not directory.is_dir():
                continue
            has_report = any(
                (directory / name).is_file() for name in _JUDGE_REPORT_NAMES
            )
            has_traj = any(
                (directory / name).is_file() for name in _TRAJECTORY_NAMES
            )
            if has_report or has_traj:
                seen.add(directory)
                found.append(directory)
    return found


def resolve_cua_bundle(job_dir: Path, task_path: Path | None = None) -> CuaArtifactBundle | None:
    """Locate CUA artifacts under a Harbor job directory."""
    dirs = find_cua_artifact_dirs(job_dir)
    if not dirs:
        return None

    verifier_dir = dirs[0]
    judge_path = None
    for name in _JUDGE_REPORT_NAMES:
        candidate = verifier_dir / name
        if candidate.is_file():
            judge_path = candidate
            break
    judge_report = _load_json(judge_path) if judge_path else None

    traj_path = None
    for name in _TRAJECTORY_NAMES:
        candidate = verifier_dir / name
        if candidate.is_file():
            traj_path = candidate
            break

    loop_model = None
    judge_model = None
    if isinstance(judge_report, dict):
        loop_model = (
            str(judge_report.get("verifier_model") or "").strip() or None
        )
        judge_model = str(judge_report.get("judge_model") or "").strip() or None
    if loop_model is None and traj_path is not None:
        traj = _load_json(traj_path)
        if isinstance(traj, dict):
            agent = traj.get("agent")
            if isinstance(agent, dict):
                loop_model = (
                    str(agent.get("model_name") or "").strip() or None
                )
    if loop_model is None and task_path is not None:
        cfg = load_cua_model_config(task_path)
        loop_model = cfg.get("model")
        judge_model = judge_model or cfg.get("judge_model")
    if judge_model is None:
        judge_model = loop_model

    return CuaArtifactBundle(
        verifier_dir=verifier_dir,
        trajectory_path=traj_path,
        judge_report_path=judge_path,
        judge_report=judge_report,
        loop_model=loop_model,
        judge_model=judge_model,
    )


def task_has_cua_signals(task_path: Path) -> bool:
    """True when the task bundle configures an inline or Harbor CUA verifier."""
    if not task_path or not task_path.exists():
        return False
    if (task_path / "tests" / "cua_config.json").is_file():
        return True
    if (task_path / "tests" / "cua_verifier.py").is_file():
        return True
    toml_path = task_path / "task.toml"
    if not toml_path.is_file():
        return False
    try:
        text = toml_path.read_text(encoding="utf-8")
    except OSError:
        return False
    lowered = text.lower()
    if re.search(r'type\s*=\s*["\']cua["\']', lowered):
        return True
    if "[verifier.cua]" in lowered or "[[verifiers]]" in lowered and 'type = "cua"' in lowered:
        return True
    return False


def load_cua_model_config(task_path: Path) -> dict[str, str | None]:
    """Best-effort model / judge_model from cua_config.json or task.toml."""
    out: dict[str, str | None] = {"model": None, "judge_model": None}
    cfg_path = task_path / "tests" / "cua_config.json"
    if cfg_path.is_file():
        data = _load_json(cfg_path)
        if data:
            model = data.get("model")
            judge = data.get("judge_model")
            out["model"] = str(model).strip() if model else None
            out["judge_model"] = str(judge).strip() if judge else out["model"]
            return out
    toml_path = task_path / "task.toml"
    if not toml_path.is_file():
        return out
    try:
        text = toml_path.read_text(encoding="utf-8")
    except OSError:
        return out
    # Prefer the dedicated [verifier.cua] table when present.
    m = re.search(
        r"\[verifier\.cua\][^\[]*?model\s*=\s*[\"']([^\"']+)[\"']",
        text,
        re.IGNORECASE | re.DOTALL,
    )
    if m:
        out["model"] = m.group(1).strip()
    j = re.search(
        r"\[verifier\.cua\][^\[]*?judge_model\s*=\s*[\"']([^\"']+)[\"']",
        text,
        re.IGNORECASE | re.DOTALL,
    )
    if j:
        out["judge_model"] = j.group(1).strip()
    if out["model"] is None and out["judge_model"] is None:
        # Harbor multi-stage: [[verifiers]] with type = "cua".
        for block in re.split(r"\[\[verifiers\]\]", text, flags=re.IGNORECASE)[1:]:
            if not re.search(r'type\s*=\s*["\']cua["\']', block, re.IGNORECASE):
                continue
            bm = re.search(
                r"(?:^|\n)\s*model\s*=\s*[\"']([^\"']+)[\"']",
                block,
                re.IGNORECASE,
            )
            bj = re.search(
                r"(?:^|\n)\s*judge_model\s*=\s*[\"']([^\"']+)[\"']",
                block,
                re.IGNORECASE,
            )
            if bm:
                out["model"] = bm.group(1).strip()
            if bj:
                out["judge_model"] = bj.group(1).strip()
            if out["model"] or out["judge_model"]:
                break
    if out["judge_model"] is None:
        out["judge_model"] = out["model"]
    return out


def _loop_draft(
    *,
    model: str | None,
    route: str,
    key_hash: str | None,
    input_tokens: int | None = None,
    output_tokens: int | None = None,
    cache_read_tokens: int | None = None,
    cache_write_tokens: int | None = None,
    cost_usd: float | None = None,
    cost_source: str = COST_ESTIMATED,
    unpriced_reason: str | None = None,
) -> VerifierCostDraft:
    return VerifierCostDraft(
        component=COMPONENT_LOOP,
        model=model,
        route=route,
        llm_key_hash=key_hash,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cache_read_tokens=cache_read_tokens,
        cache_write_tokens=cache_write_tokens,
        cost_usd=cost_usd,
        cost_source=cost_source,
        unpriced_reason=unpriced_reason,
    )


def draft_loop_cost(bundle: CuaArtifactBundle) -> VerifierCostDraft:
    model = bundle.loop_model
    route = infer_verifier_route(model)
    key_hash = _key_hash_for_route(route)
    if bundle.trajectory_path is None or not bundle.trajectory_path.is_file():
        return _loop_draft(
            model=model,
            route=route,
            key_hash=key_hash,
            unpriced_reason=UNPRICED_MISSING_TRAJECTORY,
        )
    data = _load_json(bundle.trajectory_path)
    if not data:
        return _loop_draft(
            model=model,
            route=route,
            key_hash=key_hash,
            unpriced_reason=UNPRICED_MISSING_TRAJECTORY,
        )
    inp, out, cache, cache_write, cost = _metrics_from_atif(data)
    if cost is not None and cost > 0:
        return _loop_draft(
            model=model,
            route=route,
            key_hash=key_hash,
            input_tokens=inp,
            output_tokens=out,
            cache_read_tokens=cache,
            cache_write_tokens=cache_write,
            cost_usd=cost,
            cost_source=COST_NATIVE,
        )
    estimated = estimate_cost_usd(model, inp, out, cache, cache_write)
    if estimated is not None:
        return _loop_draft(
            model=model,
            route=route,
            key_hash=key_hash,
            input_tokens=inp,
            output_tokens=out,
            cache_read_tokens=cache,
            cache_write_tokens=cache_write,
            cost_usd=estimated,
        )
    return _loop_draft(
        model=model,
        route=route,
        key_hash=key_hash,
        input_tokens=inp,
        output_tokens=out,
        cache_read_tokens=cache,
        cache_write_tokens=cache_write,
        unpriced_reason=UNPRICED_MISSING_USAGE,
    )


def _judge_criteria_count(report: dict[str, Any]) -> int:
    """How many rubric rows the judge priced — list or map shaped reports."""
    verdicts = report.get("verdicts")
    if isinstance(verdicts, list):
        return max(len(verdicts), 1)
    if isinstance(verdicts, dict) and verdicts:
        return len(verdicts)
    for key in ("criteria", "cua_criteria", "results"):
        block = report.get(key)
        if isinstance(block, list) and block:
            return len(block)
        if isinstance(block, dict) and block:
            return len(block)
    return 1


def draft_judge_cost(bundle: CuaArtifactBundle) -> VerifierCostDraft | None:
    report = bundle.judge_report
    if not isinstance(report, dict):
        return None
    model = bundle.judge_model or bundle.loop_model
    route = infer_verifier_route(model)
    key_hash = _key_hash_for_route(route)
    criteria_count = _judge_criteria_count(report)
    inp = _JUDGE_INPUT_TOKENS_PER_CRITERION * criteria_count
    out = _JUDGE_OUTPUT_TOKENS_PER_CRITERION * criteria_count
    estimated = estimate_cost_usd(model, inp, out, 0, 0)
    return VerifierCostDraft(
        component=COMPONENT_JUDGE,
        model=model,
        route=route,
        llm_key_hash=key_hash,
        input_tokens=inp,
        output_tokens=out,
        cache_read_tokens=0,
        cache_write_tokens=0,
        cost_usd=estimated,
        cost_source=COST_ESTIMATED,
        unpriced_reason=None if estimated is not None else UNPRICED_MISSING_USAGE,
    )


def build_verifier_cost_drafts(
    job_dir: Path, task_path: Path | None = None
) -> list[VerifierCostDraft]:
    """Return loop (+ judge) drafts when CUA artifacts are present."""
    if task_path is not None and not task_has_cua_signals(task_path):
        return []
    bundle = resolve_cua_bundle(job_dir, task_path=task_path)
    if bundle is None:
        return []
    drafts = [draft_loop_cost(bundle)]
    judge = draft_judge_cost(bundle)
    if judge is not None:
        drafts.append(judge)
    return drafts
