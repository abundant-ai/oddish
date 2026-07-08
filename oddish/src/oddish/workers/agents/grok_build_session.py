"""Build a rich Grok Build trajectory from the CLI's on-disk session store.

Grok's headless output (``grok -p --output-format json|streaming-json``) only
streams the assistant's visible ``text``/``thought`` plus a final ``end`` event
-- it carries **no tool calls and no token usage** (see the Grok CLI
``14-headless-mode.md`` docs). The full trajectory (tool calls, results,
per-turn token usage) is persisted by the CLI under
``$GROK_HOME/sessions/<url-encoded-cwd>/<session-id>/`` as newline-delimited
JSON:

- ``updates.jsonl``  -- the ACP ``session/update`` stream (agent message chunks,
  ``tool_call`` / ``tool_call_update`` entries): the richest ordered trajectory.
- ``events.jsonl``   -- lifecycle events (``turn_started`` / ``tool_started`` /
  ``tool_completed`` / ``turn_ended``), sometimes carrying token usage.
- ``chat_history.jsonl`` / ``summary.json`` -- conversation + metadata.

:class:`OddishGrokBuild` copies that directory into
``/logs/agent/grok-session`` so it survives sandbox teardown; this module turns
it into an ATIF :class:`Trajectory` (with ``tool_calls`` and
:class:`FinalMetrics` token totals). Everything is best-effort and defensive:
any parse failure returns ``None`` / zero so the caller falls back to the
stdout-derived trajectory rather than failing the trial.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from harbor.models.trajectories.agent import Agent
from harbor.models.trajectories.final_metrics import FinalMetrics
from harbor.models.trajectories.observation import Observation
from harbor.models.trajectories.observation_result import ObservationResult
from harbor.models.trajectories.step import Step
from harbor.models.trajectories.tool_call import ToolCall
from harbor.models.trajectories.trajectory import Trajectory


def _observation(content: str, *, source_call_id: str | None = None) -> Observation:
    return Observation(
        results=[ObservationResult(source_call_id=source_call_id, content=content)]
    )

# Session-relative directory the agent copies the Grok session store into.
GROK_SESSION_CAPTURE_DIRNAME = "grok-session"

# Token-usage field aliases seen across Grok's Chat Completions / Responses /
# Messages payloads and its own session updates. Values are summed per alias
# group so a single logical count is not double-counted.
_PROMPT_TOKEN_KEYS = ("prompt_tokens", "input_tokens", "promptTokens", "inputTokens")
_COMPLETION_TOKEN_KEYS = (
    "completion_tokens",
    "output_tokens",
    "completionTokens",
    "outputTokens",
)
_CACHE_TOKEN_KEYS = (
    "cached_tokens",
    "cache_read_input_tokens",
    "cache_read_tokens",
    "cachedTokens",
)


def _iter_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    out: list[dict[str, Any]] = []
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict):
            out.append(obj)
    return out


def find_session_dir(capture_root: Path, session_id: str | None) -> Path | None:
    """Locate the concrete session directory inside a captured ``grok-session``.

    The capture copies ``$GROK_HOME/sessions`` (and ``logs``) verbatim, so the
    real per-session directory is nested as ``sessions/<enc-cwd>/<session-id>/``.
    Prefer an exact ``session_id`` match.

    When ``session_id`` is known but no directory matches it, we must NOT blindly
    pick the most recent session -- that could attribute a different Grok
    session's steps/tokens to this trial. A trial runs in a fresh sandbox with a
    single session, so a lone candidate is unambiguously the trial's session and
    is safe to use; with multiple candidates and no exact match we refuse to
    guess (return ``None``) so the caller falls back to the stdout trajectory.

    When ``session_id`` is unknown (``None``), fall back to the most recently
    modified directory that contains an ``updates.jsonl`` or ``events.jsonl``.
    """
    if not capture_root.is_dir():
        return None
    candidates: list[Path] = []
    for path in capture_root.rglob("*"):
        if not path.is_dir():
            continue
        if session_id and path.name == session_id:
            return path
        if (path / "updates.jsonl").is_file() or (path / "events.jsonl").is_file():
            candidates.append(path)
    if not candidates:
        return None
    if session_id:
        # Known session id but no exact directory match: only trust a single
        # unambiguous session; never guess between several.
        return candidates[0] if len(candidates) == 1 else None
    candidates.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    return candidates[0]


def _scan_tokens(obj: Any, totals: dict[str, int]) -> None:
    """Recursively accumulate the largest token counts found under any key group.

    Usage blocks are emitted per-turn and often repeated (deltas + final), so we
    keep the running MAX per group rather than summing to avoid inflating counts
    from cumulative re-reports within a single turn's nested payloads.
    """
    if isinstance(obj, dict):
        for group, keys in (
            ("prompt", _PROMPT_TOKEN_KEYS),
            ("completion", _COMPLETION_TOKEN_KEYS),
            ("cache", _CACHE_TOKEN_KEYS),
        ):
            for key in keys:
                value = obj.get(key)
                if isinstance(value, bool):
                    continue
                if isinstance(value, int) and value >= 0:
                    totals[group] = max(totals.get(group, 0), value)
        for value in obj.values():
            _scan_tokens(value, totals)
    elif isinstance(obj, list):
        for item in obj:
            _scan_tokens(item, totals)


def _sum_tokens_over(paths: list[Path]) -> dict[str, int]:
    prompt = completion = cache = 0
    for path in paths:
        for record in _iter_jsonl(path):
            per: dict[str, int] = {}
            _scan_tokens(record, per)
            prompt += per.get("prompt", 0)
            completion += per.get("completion", 0)
            cache += per.get("cache", 0)
        if prompt or completion or cache:
            # Prefer the first source that carries usage; avoid double counting
            # the same turns across multiple files that mirror the same records.
            break
    return {"prompt": prompt, "completion": completion, "cache": cache}


def extract_token_totals(
    session_dir: Path, *, log_dirs: list[Path] | None = None
) -> dict[str, int]:
    """Best-effort per-run token totals summed across turns.

    Each turn re-reports its own usage; we take the max within a turn's payload
    (via :func:`_scan_tokens`) and sum across the ordered turn/usage records.
    The session stream (``events.jsonl`` / ``updates.jsonl``) sometimes omits
    split token usage; grok's sampling log under ``$GROK_HOME/logs`` records it
    per request, so we fall back to any ``*.jsonl`` there.
    """
    totals = _sum_tokens_over(
        [
            session_dir / "events.jsonl",
            session_dir / "updates.jsonl",
            session_dir / "chat_history.jsonl",
        ]
    )
    if totals["prompt"] or totals["completion"] or totals["cache"]:
        return totals
    for log_dir in log_dirs or []:
        if not log_dir.is_dir():
            continue
        log_totals = _sum_tokens_over(sorted(log_dir.glob("*.jsonl")))
        if log_totals["prompt"] or log_totals["completion"] or log_totals["cache"]:
            return log_totals
    return totals


def _update_payload(record: dict[str, Any]) -> dict[str, Any]:
    params = record.get("params")
    if isinstance(params, dict):
        update = params.get("update")
        if isinstance(update, dict):
            return update
    update = record.get("update")
    if isinstance(update, dict):
        return update
    return {}


def _chunk_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, dict):
        for key in ("text", "content", "data"):
            value = content.get(key)
            if isinstance(value, str):
                return value
    if isinstance(content, list):
        return "".join(_chunk_text(item) for item in content)
    return ""


def build_trajectory_from_updates(
    session_dir: Path,
    *,
    session_id: str | None,
    agent_version: str,
    model_name: str | None,
    log_dirs: list[Path] | None = None,
) -> Trajectory | None:
    """Convert ``updates.jsonl`` (ACP session stream) into an ATIF trajectory.

    Recognized ``sessionUpdate`` kinds: ``agent_message_chunk`` /
    ``agent_thought_chunk`` (assistant text / reasoning), ``tool_call`` (a new
    tool invocation) and ``tool_call_update`` (its result/status). Unknown kinds
    are ignored. Returns ``None`` when nothing usable is present.
    """
    updates = _iter_jsonl(session_dir / "updates.jsonl")
    if not updates:
        return None

    steps: list[Step] = []
    pending_text: list[str] = []
    pending_reasoning: list[str] = []
    tool_calls_by_id: dict[str, int] = {}

    def flush_text() -> None:
        if pending_reasoning:
            steps.append(
                Step(
                    step_id=len(steps) + 1,
                    source="agent",
                    model_name=model_name,
                    message="Reasoning",
                    reasoning_content="".join(pending_reasoning),
                    llm_call_count=1,
                    extra={"source": "grok_session", "kind": "thought"},
                )
            )
            pending_reasoning.clear()
        if pending_text:
            steps.append(
                Step(
                    step_id=len(steps) + 1,
                    source="agent",
                    model_name=model_name,
                    message="".join(pending_text),
                    llm_call_count=1,
                    extra={"source": "grok_session", "kind": "text"},
                )
            )
            pending_text.clear()

    for record in updates:
        update = _update_payload(record)
        kind = str(update.get("sessionUpdate") or update.get("type") or "").strip()
        if kind in ("agent_message_chunk", "agent_message"):
            pending_text.append(_chunk_text(update.get("content")))
        elif kind in ("agent_thought_chunk", "agent_thought", "thought"):
            pending_reasoning.append(_chunk_text(update.get("content")))
        elif kind == "tool_call":
            flush_text()
            tool_id = str(
                update.get("toolCallId") or update.get("tool_call_id") or len(steps)
            )
            name = str(
                update.get("title") or update.get("kind") or update.get("name") or "tool"
            )
            raw_input = update.get("rawInput")
            arguments = raw_input if isinstance(raw_input, dict) else {}
            steps.append(
                Step(
                    step_id=len(steps) + 1,
                    source="agent",
                    model_name=model_name,
                    message=name,
                    tool_calls=[
                        ToolCall(
                            tool_call_id=tool_id,
                            function_name=name,
                            arguments=arguments,
                        )
                    ],
                    llm_call_count=1,
                    extra={"source": "grok_session", "kind": "tool_call"},
                )
            )
            tool_calls_by_id[tool_id] = len(steps) - 1
        elif kind == "tool_call_update":
            tool_id = str(update.get("toolCallId") or update.get("tool_call_id") or "")
            status = update.get("status")
            content = update.get("content")
            observation_text = _chunk_text(content)
            idx = tool_calls_by_id.get(tool_id)
            if idx is not None and observation_text:
                existing = steps[idx]
                if existing.observation is None:
                    existing.observation = _observation(
                        observation_text, source_call_id=tool_id or None
                    )
            elif observation_text:
                steps.append(
                    Step(
                        step_id=len(steps) + 1,
                        source="user",
                        message="tool result"
                        + (f" ({status})" if status else ""),
                        observation=_observation(
                            observation_text, source_call_id=tool_id or None
                        ),
                        extra={"source": "grok_session", "kind": "tool_result"},
                    )
                )

    flush_text()
    if not steps:
        return None

    totals = extract_token_totals(session_dir, log_dirs=log_dirs)
    final_metrics = FinalMetrics(
        total_prompt_tokens=totals["prompt"] or None,
        total_completion_tokens=totals["completion"] or None,
        total_cached_tokens=totals["cache"] or None,
        total_steps=len(steps),
        extra={"source": "grok_session"},
    )
    return Trajectory(
        schema_version="ATIF-v1.7",
        session_id=session_id,
        agent=Agent(
            name="grok-build",
            version=agent_version,
            model_name=model_name,
            extra={"trajectory_source": "grok_session"},
        ),
        steps=steps,
        final_metrics=final_metrics,
        extra={"trajectory_source": "grok_session"},
    )


def build_session_trajectory(
    capture_root: Path,
    *,
    session_id: str | None,
    agent_version: str,
    model_name: str | None,
) -> Trajectory | None:
    """Locate the captured session and build a rich trajectory from it."""
    session_dir = find_session_dir(capture_root, session_id)
    if session_dir is None:
        return None
    # grok writes its sampling log (per-request token usage) under a sibling
    # ``logs/`` tree; collect any such dirs so token totals can fall back to it.
    log_dirs = [p for p in capture_root.rglob("logs") if p.is_dir()]
    try:
        return build_trajectory_from_updates(
            session_dir,
            session_id=session_id,
            agent_version=agent_version,
            model_name=model_name,
            log_dirs=log_dirs,
        )
    except Exception:
        return None
