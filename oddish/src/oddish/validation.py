"""Pre-flight validation for agent/model configurations.

Catches common configuration mistakes (typo'd agent names, missing
provider prefix, bad model IDs, missing API keys) *before* a job is
submitted to the queue and starts burning compute budget on every trial.

Two layers, opt-in independently:

* ``validate_configs`` (cheap, always-on by default) — pure local checks:
  - agent name resolves against ``harbor.models.agent.name.AgentName``
  - model is present for any agent except ``nop`` / ``oracle``
  - model normalizes (no whitespace-only / placeholder strings)

* ``smoke_test_models`` (opt-in, network) — for each unique
  ``(provider, model)`` pair, makes a 1-token API call to verify that
  credentials and model identifiers are valid.

Both layers are exposed as ``ValidationReport`` so the CLI, sweep
runner, and experiment driver share one report shape and one renderer
(``render_report``). The reporter is intentionally synchronous and
returns immediately — callers that want to alert / email on failure
can pass a ``notify`` callback.

Mirrors the validator in ``task-workflows/.github/scripts/validate-agents.py``
but lives inside the Oddish package so it works in the CLI, in
``oddish run -c sweep.yaml``, and as a library call from experiments.
"""

from __future__ import annotations

import json
import os
import ssl
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Mapping

from oddish.config import normalize_model_id


# Agents that legitimately have no model (the "default" model in
# ``settings.normalize_trial_model``). Kept in a module-level set so
# ``validate_configs`` doesn't need to import settings on every call.
_MODEL_OPTIONAL_AGENTS = {"nop", "oracle"}


@dataclass(frozen=True)
class ValidationIssue:
    """A single validation problem (or success) for one agent/model entry."""

    index: int
    agent: str
    model: str | None
    severity: str  # "error" | "warning" | "ok"
    code: str
    message: str

    @property
    def ok(self) -> bool:
        return self.severity == "ok"


@dataclass
class ValidationReport:
    """Aggregate validation result. Truthy iff no errors."""

    issues: list[ValidationIssue] = field(default_factory=list)
    smoke_tested: bool = False

    @property
    def errors(self) -> list[ValidationIssue]:
        return [i for i in self.issues if i.severity == "error"]

    @property
    def warnings(self) -> list[ValidationIssue]:
        return [i for i in self.issues if i.severity == "warning"]

    @property
    def ok(self) -> bool:
        return not self.errors

    def __bool__(self) -> bool:
        return self.ok

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "smoke_tested": self.smoke_tested,
            "errors": [_issue_dict(i) for i in self.errors],
            "warnings": [_issue_dict(i) for i in self.warnings],
            "issues": [_issue_dict(i) for i in self.issues],
        }


def _issue_dict(issue: ValidationIssue) -> dict[str, Any]:
    return {
        "index": issue.index,
        "agent": issue.agent,
        "model": issue.model,
        "severity": issue.severity,
        "code": issue.code,
        "message": issue.message,
    }


# ---------------------------------------------------------------------------
# Agent name discovery
# ---------------------------------------------------------------------------


def get_valid_agent_names() -> set[str] | None:
    """Return Harbor's known agent names, or ``None`` if Harbor is unavailable.

    Returning ``None`` lets callers degrade gracefully (skip agent-name
    validation rather than fail loud) — the same approach
    ``task-workflows/validate-agents.py`` takes when probing harbor.
    """
    try:
        from harbor.models.agent.name import AgentName  # type: ignore
    except Exception:
        return None
    return {a.value for a in AgentName}


# ---------------------------------------------------------------------------
# Local (cheap) validation
# ---------------------------------------------------------------------------


def _coerce_entry(entry: Mapping[str, Any] | Any) -> tuple[str, str | None]:
    """Pull (agent, model) out of a sweep config / TrialSpec / dict-ish.

    Accepts the multiple shapes Oddish hands around:

    - sweep config dict from ``load_sweep_config`` (``{"agent": ..., "model": ...}``
      or Harbor-style ``{"name": ..., "model_name": ...}``).
    - ``TrialSpec`` / ``AgentModelPair`` Pydantic instance.
    - Plain dict from JSON.

    Anything missing comes back as ``""`` / ``None`` so the caller can
    flag a structural error.
    """
    if hasattr(entry, "model_dump"):
        data = entry.model_dump()  # pydantic
    elif isinstance(entry, Mapping):
        data = dict(entry)
    else:
        return ("", None)

    agent = data.get("agent") or data.get("name") or ""
    model = data.get("model")
    if model is None:
        model = data.get("model_name")
    return (str(agent), model if model is None else str(model))


def validate_configs(
    configs: Iterable[Mapping[str, Any] | Any],
) -> ValidationReport:
    """Cheap local validation — no network calls.

    Checks each config has a recognizable agent name and a model where
    one is required. Always returns; never raises.
    """
    valid_agents = get_valid_agent_names()
    report = ValidationReport()

    for index, entry in enumerate(configs):
        agent, model = _coerce_entry(entry)
        normalized_model = normalize_model_id(model)

        if not agent:
            report.issues.append(
                ValidationIssue(
                    index=index,
                    agent=agent,
                    model=model,
                    severity="error",
                    code="agent_missing",
                    message="agent is required (use 'agent' or 'name' field)",
                )
            )
            continue

        agent_lc = agent.strip().lower()

        if valid_agents is None:
            report.issues.append(
                ValidationIssue(
                    index=index,
                    agent=agent,
                    model=normalized_model,
                    severity="warning",
                    code="agent_check_skipped",
                    message="harbor not importable; skipping agent-name validation",
                )
            )
        elif agent_lc not in valid_agents:
            suggestion = _did_you_mean(agent_lc, valid_agents)
            hint = f" Did you mean '{suggestion}'?" if suggestion else ""
            report.issues.append(
                ValidationIssue(
                    index=index,
                    agent=agent,
                    model=normalized_model,
                    severity="error",
                    code="agent_unknown",
                    message=(
                        f"agent '{agent}' is not a Harbor built-in agent."
                        f"{hint} Valid: {sorted(valid_agents)}"
                    ),
                )
            )
            continue

        if agent_lc in _MODEL_OPTIONAL_AGENTS:
            report.issues.append(
                ValidationIssue(
                    index=index,
                    agent=agent,
                    model=normalized_model,
                    severity="ok",
                    code="ok",
                    message=f"{agent} (no model required)",
                )
            )
            continue

        if not normalized_model:
            report.issues.append(
                ValidationIssue(
                    index=index,
                    agent=agent,
                    model=model,
                    severity="error",
                    code="model_missing",
                    message=(
                        f"agent '{agent}' requires a model. "
                        "Set 'model' (CLI: -m / sweep: model_name)."
                    ),
                )
            )
            continue

        if "/" not in normalized_model:
            # Bare model — Oddish/litellm can usually infer the provider,
            # but a missing prefix is the most common typo source. Warn,
            # don't error.
            report.issues.append(
                ValidationIssue(
                    index=index,
                    agent=agent,
                    model=normalized_model,
                    severity="warning",
                    code="model_no_provider_prefix",
                    message=(
                        f"model '{normalized_model}' has no provider prefix "
                        "(e.g. 'anthropic/...'). Provider will be inferred."
                    ),
                )
            )
        else:
            report.issues.append(
                ValidationIssue(
                    index=index,
                    agent=agent,
                    model=normalized_model,
                    severity="ok",
                    code="ok",
                    message=f"{agent} + {normalized_model}",
                )
            )

    return report


def _did_you_mean(target: str, candidates: Iterable[str]) -> str | None:
    """Return the closest candidate by simple ratio, or None if nothing close."""
    import difflib

    matches = difflib.get_close_matches(target, list(candidates), n=1, cutoff=0.6)
    return matches[0] if matches else None


# ---------------------------------------------------------------------------
# API smoke-test (opt-in, network)
# ---------------------------------------------------------------------------


def _resolve_provider(model: str) -> tuple[str, str]:
    """Map a normalized model string to (provider, model_id).

    Provider keys match the smoke-test validators below. Bedrock model
    IDs (``us.anthropic.claude-*``) are mapped to ``anthropic`` because
    we validate the Anthropic API key (the routing happens at runtime).
    """
    if "/" in model:
        prefix, rest = model.split("/", 1)
        prefix = prefix.lower()
        if prefix in ("anthropic", "bedrock", "claude"):
            return ("anthropic", rest)
        if prefix in ("google", "gemini", "vertex_ai"):
            return ("gemini", rest)
        if prefix in ("xai",):
            return ("xai", rest)
        return ("openai", rest)

    lowered = model.lower()
    if lowered.startswith("claude"):
        return ("anthropic", model)
    if lowered.startswith("gemini"):
        return ("gemini", model)
    if lowered.startswith("grok"):
        return ("xai", model)
    return ("openai", model)


def _http_post(url: str, headers: dict, body: dict, timeout: int = 20) -> tuple[bool, str]:
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    ctx = ssl.create_default_context()
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=ctx) as resp:
            resp.read(1024)
        return (True, "ok")
    except urllib.error.HTTPError as exc:
        body_text = ""
        try:
            body_text = exc.read().decode("utf-8", errors="replace")[:300]
        except Exception:
            pass
        return (False, f"HTTP {exc.code}: {body_text}")
    except Exception as exc:
        return (False, str(exc)[:200])


def _probe_anthropic(model_id: str) -> tuple[bool, str]:
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        return (False, "ANTHROPIC_API_KEY not set")

    # Bedrock-style IDs aren't accepted by the direct API; we still
    # validate the key with a known-good model so the user finds out
    # *now* rather than after every trial fails to authenticate.
    probe_model = model_id
    if probe_model.startswith(("us.", "eu.")):
        probe_model = "claude-haiku-4-5-20251001"

    return _http_post(
        "https://api.anthropic.com/v1/messages",
        {
            "x-api-key": api_key,
            "Content-Type": "application/json",
            "anthropic-version": "2023-06-01",
        },
        {
            "model": probe_model,
            "messages": [{"role": "user", "content": "ok"}],
            "max_tokens": 1,
        },
    )


def _probe_openai(model_id: str) -> tuple[bool, str]:
    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key:
        return (False, "OPENAI_API_KEY not set")
    base_url = os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1").rstrip("/")
    return _http_post(
        f"{base_url}/responses",
        {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        {"model": model_id, "input": "ok", "max_output_tokens": 16},
    )


def _probe_gemini(model_id: str) -> tuple[bool, str]:
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        return (False, "GEMINI_API_KEY not set")
    url = (
        f"https://generativelanguage.googleapis.com/v1beta/models/"
        f"{model_id}:generateContent?key={api_key}"
    )
    return _http_post(
        url,
        {"Content-Type": "application/json"},
        {
            "contents": [{"parts": [{"text": "ok"}]}],
            "generationConfig": {"maxOutputTokens": 1},
        },
    )


def _probe_xai(model_id: str) -> tuple[bool, str]:
    api_key = os.environ.get("XAI_API_KEY")
    if not api_key:
        return (False, "XAI_API_KEY not set")
    return _http_post(
        "https://api.x.ai/v1/chat/completions",
        {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        {
            "model": model_id,
            "messages": [{"role": "user", "content": "ok"}],
            "max_tokens": 1,
        },
    )


_PROBES: dict[str, Callable[[str], tuple[bool, str]]] = {
    "anthropic": _probe_anthropic,
    "openai": _probe_openai,
    "gemini": _probe_gemini,
    "xai": _probe_xai,
}


def smoke_test_models(report: ValidationReport) -> ValidationReport:
    """Augment ``report`` with live API probes for each unique model.

    Skips entries that already have errors locally — no point asking
    OpenAI to confirm a typo we already caught. Mutates and returns
    the report.
    """
    seen: dict[tuple[str, str], list[int]] = {}
    for issue in report.issues:
        if issue.severity == "error":
            continue
        if not issue.model or issue.agent.strip().lower() in _MODEL_OPTIONAL_AGENTS:
            continue
        provider, model_id = _resolve_provider(issue.model)
        seen.setdefault((provider, model_id), []).append(issue.index)

    for (provider, model_id), indices in seen.items():
        probe = _PROBES.get(provider)
        if not probe:
            for idx in indices:
                report.issues.append(
                    ValidationIssue(
                        index=idx,
                        agent="?",
                        model=f"{provider}/{model_id}",
                        severity="warning",
                        code="probe_unknown_provider",
                        message=f"no smoke-test probe for provider '{provider}'",
                    )
                )
            continue

        success, msg = probe(model_id)
        severity = "ok" if success else "error"
        code = "smoke_ok" if success else "smoke_failed"
        for idx in indices:
            report.issues.append(
                ValidationIssue(
                    index=idx,
                    agent="?",
                    model=f"{provider}/{model_id}",
                    severity=severity,
                    code=code,
                    message=f"{provider}/{model_id}: {msg}",
                )
            )

    report.smoke_tested = True
    return report


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------


def render_report(report: ValidationReport) -> str:
    """Format a report for human consumption.

    Used by the CLI; experiments or notification hooks can call this to
    build the body of an alert/email.
    """
    lines: list[str] = []
    if report.ok:
        lines.append("Validation passed.")
    else:
        lines.append(f"Validation FAILED: {len(report.errors)} error(s).")

    for issue in report.issues:
        marker = {"error": "✗", "warning": "!", "ok": "✓"}.get(issue.severity, "·")
        prefix = f"  {marker} [{issue.code}] "
        suffix = f" (#{issue.index} agent={issue.agent} model={issue.model})"
        lines.append(prefix + issue.message + suffix)

    if not report.ok:
        lines.append("")
        lines.append("Fix and re-run, or pass --no-validate to skip these checks.")
    return "\n".join(lines)


def validate_and_report(
    configs: Iterable[Mapping[str, Any] | Any],
    *,
    smoke_test: bool = False,
    notify: Callable[[ValidationReport], None] | None = None,
) -> ValidationReport:
    """One-shot helper: run validation, optionally smoke-test, fire ``notify``.

    Returns the report; raises nothing. Callers (CLI / experiment
    driver) decide what to do with a non-OK report.
    """
    report = validate_configs(configs)
    if smoke_test:
        smoke_test_models(report)
    if notify is not None and not report.ok:
        try:
            notify(report)
        except Exception:
            # Don't let a flaky notifier (email outage, etc.) mask the
            # underlying validation failure.
            pass
    return report
