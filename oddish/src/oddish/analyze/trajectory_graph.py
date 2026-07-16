"""Agent-graph builder: condense an ATIF trajectory into a step graph.

The trajectory viewer shows every ATIF step (each tool call, each message). That
is faithful but hard to read: to understand *what the agent actually did* on a
run you have to scroll dozens of steps. This module processes a trajectory into a
handful of GENERAL phases (explore -> reproduce -> patch -> verify ...), each
tagged ok/warn/error, plus a single terminal node = the last thing the agent did
and why the run ended (passed, failed the grader, hit its timeout, crashed).

It reuses the log-analysis LLM path: a small call on ``settings.analysis_model``
through the ``oddish.core.llm`` funnel (direct Anthropic API, model normalized
back to a direct-API id). The run's *outcome* is never
taken from the LLM -- it is computed deterministically from the trial's own
``status`` / ``reward`` / ``error_message`` so the terminal node can't disagree
with the grader. The LLM only narrates the phases and the last action.

If there is no trajectory, no API key, or the LLM errors, a deterministic
heuristic (segment steps by tool-call clusters) still yields a usable graph, so
the endpoint always returns something and works in local dev without a key.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any

logger = logging.getLogger(__name__)

# Bump when the graph shape changes so persisted graphs are regenerated on read
# (see ``core.trial_io._graph_is_fresh``).
GRAPH_SCHEMA_VERSION = "1"

# Terminal outcomes. Kept in sync with the frontend renderer's node styling AND
# with the app's own trial classification (frontend `getMatrixStatus`): a full
# pass, partial credit, a scoreless (verification-off) completion, a grader
# failure, a timeout, a harness/infra error, a skip, or still running.
OUTCOME_SUCCESS = "success"
OUTCOME_PARTIAL = "partial"
OUTCOME_FAILURE = "failure"
OUTCOME_TIMEOUT = "timeout"
OUTCOME_ERROR = "error"
OUTCOME_SCORELESS = "scoreless"
OUTCOME_SKIPPED = "skipped"
OUTCOME_RUNNING = "running"
OUTCOME_QUEUED = "queued"
OUTCOME_PENDING = "pending"

# Bounds so a huge trajectory can't blow the analysis token budget.
_MAX_STEPS_IN_DIGEST = 160
_MAX_FIELD_CHARS = 240
_MAX_GRAPH_STEPS = 12

_GRAPH_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["headline", "steps", "terminal"],
    "properties": {
        "headline": {
            "type": "string",
            "description": "One line: what the agent was trying to do and how it ended.",
        },
        "steps": {
            "type": "array",
            "maxItems": _MAX_GRAPH_STEPS,
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["title", "detail", "status"],
                "properties": {
                    "title": {
                        "type": "string",
                        "description": "3-6 word phase name (e.g. 'Reproduce the failing test').",
                    },
                    "detail": {
                        "type": "string",
                        "description": "One sentence: what happened in this phase.",
                    },
                    "status": {
                        "type": "string",
                        "enum": ["ok", "warn", "error"],
                        "description": "ok = went fine; warn = hit a snag but recovered; error = went wrong here.",
                    },
                },
            },
        },
        "terminal": {
            "type": "object",
            "additionalProperties": False,
            "required": ["last_action", "reason"],
            "properties": {
                "last_action": {
                    "type": "string",
                    "description": "Short: the last thing the agent did before the run ended.",
                },
                "reason": {
                    "type": "string",
                    "description": "One sentence: why the run ended this way.",
                },
            },
        },
    },
}


def _reward_outcome(reward: float) -> str:
    """Reward -> outcome, matching the app's ``getRewardMatrixStatus`` EXACTLY:
    reward == 1 is a pass, reward == 0 is a failure, everything else (including
    out-of-band values like 1.5 or -0.2) is partial. Kept identical so the graph
    terminal never disagrees with the trial matrix."""
    if reward == 1:
        return OUTCOME_SUCCESS
    if reward == 0:
        return OUTCOME_FAILURE
    return OUTCOME_PARTIAL


def _mark_terminal_phase(steps: list[dict[str, Any]], outcome: str) -> None:
    """For a failing outcome, ensure SOME phase is flagged as the error.

    Respects an error the model already located (so the LLM path can point at an
    earlier phase where it went wrong); only when nothing is flagged does it fall
    back to marking the final phase. Applied on every path so a failed run never
    renders all-green phases. Partial/scoreless are not painted red."""
    if (
        steps
        and outcome in (OUTCOME_FAILURE, OUTCOME_TIMEOUT, OUTCOME_ERROR)
        and not any(s.get("status") == "error" for s in steps)
    ):
        steps[-1]["status"] = "error"


def _infer_outcome(ctx: dict[str, Any]) -> str:
    """Authoritative terminal outcome from the trial's own fields (not the LLM).

    Mirrors the frontend ``getMatrixStatus`` so the graph terminal can never
    contradict the trial drawer / grader: skipped; harness error (or timeout)
    when an error message is present, except an agent-timeout that still scored;
    a FAILED status is a harness error; a SUCCESS status maps by reward
    (pass / partial / fail), or scoreless when verification produced no reward.
    """
    status = (ctx.get("status") or "").lower()
    err = ctx.get("error_message") or ""
    reward = ctx.get("reward")
    has_reward = reward is not None
    # Agent timeouts are detected the same way the frontend does.
    is_agent_timeout = (
        "AgentTimeoutError" in err or "Agent execution timed out" in err
    )

    # Precedence follows getMatrixStatus EXACTLY (skipped -> error-message ->
    # failed -> success -> non-terminal) so the graph terminal can't diverge
    # from the trial drawer even in edge cases (e.g. a status that carries both
    # a running flag and an error message).
    if status == "skipped":
        return OUTCOME_SKIPPED

    # An error message means the run did not complete cleanly -> harness error,
    # or (more specifically than the drawer) a timeout, unless it still scored.
    if err and not (is_agent_timeout and has_reward):
        return OUTCOME_TIMEOUT if is_agent_timeout else OUTCOME_ERROR

    if status == "failed":
        if is_agent_timeout and has_reward:
            return _reward_outcome(reward)
        return OUTCOME_ERROR

    if status == "success":
        # Terminal completion: reward is the graded score; no reward = a
        # deliberately scoreless run (verification disabled), NOT an infra error.
        return _reward_outcome(reward) if has_reward else OUTCOME_SCORELESS

    # Non-terminal statuses, mapped as getMatrixStatus does: queued/pending ->
    # queued, running -> running, retrying (any other) -> pending.
    if status in ("queued", "pending"):
        return OUTCOME_QUEUED
    if status == "running":
        return OUTCOME_RUNNING
    if status == "retrying":
        return OUTCOME_PENDING

    # Unknown terminal status: fall back to the reward reading.
    return _reward_outcome(reward) if has_reward else OUTCOME_SCORELESS


def _step_tool_names(step: dict[str, Any]) -> list[str]:
    names: list[str] = []
    for call in step.get("tool_calls") or []:
        if isinstance(call, dict):
            name = call.get("function_name") or call.get("name")
            if name:
                names.append(str(name))
    return names


def _clip(text: Any, limit: int = _MAX_FIELD_CHARS) -> str:
    if not text:
        return ""
    s = str(text).strip().replace("\n", " ")
    return s if len(s) <= limit else s[: limit - 1] + "…"


def _content_text(content: Any) -> str:
    """Flatten an ATIF message/observation content (str or content-part list)."""
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for part in content:
            if isinstance(part, dict) and part.get("type") == "text":
                parts.append(part.get("text") or "")
            elif isinstance(part, str):
                parts.append(part)
        return "\n".join(parts)
    return str(content)


def _digest_steps(trajectory: dict[str, Any]) -> list[dict[str, Any]]:
    """Compact per-step records the LLM (and the heuristic) reason over."""
    steps = trajectory.get("steps") or []
    digest: list[dict[str, Any]] = []
    for idx, step in enumerate(steps):
        if not isinstance(step, dict):
            continue
        tools = _step_tool_names(step)
        obs = ""
        observation = step.get("observation")
        if isinstance(observation, dict):
            results = observation.get("results") or []
            obs = " | ".join(
                _clip(_content_text(r.get("content")), 120)
                for r in results
                if isinstance(r, dict)
            )
        digest.append(
            {
                "i": idx,
                "tools": tools,
                "message": _clip(_content_text(step.get("message"))),
                "reasoning": _clip(step.get("reasoning_content"), 160),
                "observation": _clip(obs, 160),
            }
        )
    return digest


def _transcript(digest: list[dict[str, Any]]) -> str:
    """Render the digest as a bounded text transcript for the prompt."""
    head = digest[:_MAX_STEPS_IN_DIGEST]
    lines: list[str] = []
    for d in head:
        bits = [f"#{d['i']}"]
        if d["tools"]:
            bits.append("tools=" + ",".join(d["tools"][:6]))
        if d["reasoning"]:
            bits.append("think: " + d["reasoning"])
        if d["message"]:
            bits.append("say: " + d["message"])
        if d["observation"]:
            bits.append("obs: " + d["observation"])
        lines.append("  ".join(bits))
    if len(digest) > _MAX_STEPS_IN_DIGEST:
        lines.append(f"... ({len(digest) - _MAX_STEPS_IN_DIGEST} more steps omitted)")
    return "\n".join(lines)


_OUTCOME_HINT = {
    OUTCOME_SUCCESS: "The run PASSED the grader (reward 1.0).",
    OUTCOME_PARTIAL: "The agent finished with PARTIAL credit (0 < reward < 1).",
    OUTCOME_FAILURE: "The agent finished but the grader FAILED it (reward 0).",
    OUTCOME_TIMEOUT: "The run hit its TIMEOUT before finishing.",
    OUTCOME_ERROR: "The run ended in a HARNESS/INFRA error (never graded).",
    OUTCOME_SCORELESS: "The run completed but produced NO score (verification off).",
    OUTCOME_SKIPPED: "The trial was SKIPPED (baseline gate cancelled it).",
    OUTCOME_RUNNING: "The trial is still RUNNING.",
    OUTCOME_QUEUED: "The trial is QUEUED (waiting to start).",
    OUTCOME_PENDING: "The trial is PENDING (retrying).",
}

# The last failing line the grader printed — a cheap, deterministic reason for
# the non-LLM paths (summary reuse / heuristic) so the terminal isn't generic.
_FAIL_LINE_MARKERS = (
    "fail",
    "error",
    "assert",
    "expected",
    "traceback",
    "exception",
    "not ok",
    "✗",
)


def _grounded_reason(ctx: dict[str, Any], outcome: str) -> str:
    reason = _OUTCOME_HINT.get(outcome, outcome)
    if outcome not in (OUTCOME_FAILURE, OUTCOME_TIMEOUT):
        return reason
    verifier = ctx.get("verifier_output")
    if not verifier:
        return reason
    # Surface the last grader line that reads like a failure.
    lines = [ln.strip() for ln in str(verifier).splitlines() if ln.strip()]
    for ln in reversed(lines):
        low = ln.lower()
        if any(m in low for m in _FAIL_LINE_MARKERS):
            return f"{reason} Grader: {_clip(ln, 200)}"
    return reason


def _verifier_snippet(text: Any, limit: int = 1200) -> str:
    """The tail of the verifier output — where pass/fail + assertions land."""
    if not text:
        return ""
    s = str(text).strip()
    return s if len(s) <= limit else "…" + s[-(limit - 1):]


def _build_prompt(ctx: dict[str, Any], outcome: str, transcript: str) -> str:
    n_steps = ctx.get("num_steps")
    instruction = _clip(ctx.get("task_instruction"), 1600)
    verifier = _verifier_snippet(ctx.get("verifier_output"))
    return (
        "You are summarizing what an autonomous coding/ops agent did on ONE trial "
        "of a benchmark task, so a reviewer can understand the run at a glance "
        "WITHOUT reading the full step-by-step trajectory.\n\n"
        "Produce a SMALL graph of the GENERAL phases the agent moved through -- not "
        "one node per tool call. Aim for 3 to 8 phases. Each phase is a chunk of "
        "related work (e.g. 'Explore the repo', 'Reproduce the failure', 'Patch the "
        "handler', 'Run the tests'). Judge each phase AGAINST THE TASK GOAL below, "
        "not just against whether the agent's own commands succeeded. Tag each phase:\n"
        "  ok   = made real progress toward the goal\n"
        "  warn = hit a snag but recovered / worked around it\n"
        "  error= this is where it went wrong relative to the goal (wrong hypothesis, "
        "chased a red herring, broke something, got stuck)\n\n"
        "Then a TERMINAL: the last concrete thing the agent did, and one sentence on "
        "why the run ended. Ground the terminal in the known outcome AND, when the run "
        "failed, in the grader output below -- name the specific check that failed. Do "
        "not contradict the known outcome.\n\n"
        f"## Known outcome (authoritative)\n{_OUTCOME_HINT.get(outcome, outcome)}\n"
        f"Task: {ctx.get('task_name') or 'unknown'}\n"
        f"Agent: {ctx.get('agent_name') or 'unknown'}\n"
        f"Trial status: {ctx.get('status')}  reward: {ctx.get('reward')}\n"
        f"Total trajectory steps: {n_steps}\n"
        + (
            f"Error message: {_clip(ctx.get('error_message'), 400)}\n"
            if ctx.get("error_message")
            else ""
        )
        + (
            f"\n## Task goal (instruction the agent was given)\n{instruction}\n"
            if instruction
            else ""
        )
        + (
            f"\n## Grader output (verifier stdout — why it passed/failed)\n{verifier}\n"
            if verifier
            else ""
        )
        + "\n## Trajectory (condensed, one line per step)\n"
        + (transcript or "(no trajectory steps available)")
        + "\n\nReturn ONLY the JSON object for the schema. If a phase clearly maps to a "
        "range of the steps above, reflect that ordering. The FINAL phase should be the "
        "one whose status best explains the outcome (mark it 'error' for a failure/timeout "
        "that the agent caused; 'ok' for a pass)."
    )


def _heuristic_graph(
    digest: list[dict[str, Any]], ctx: dict[str, Any], outcome: str
) -> dict[str, Any]:
    """LLM-free fallback: segment steps into phases by tool-call clusters."""
    steps: list[dict[str, Any]] = []
    if digest:
        # Group consecutive steps by their dominant tool (or "think/plan" when none).
        cur_key: str | None = None
        count = 0
        for d in digest:
            key = d["tools"][0] if d["tools"] else "reason"
            if key != cur_key:
                if cur_key is not None:
                    steps.append(_phase_from(cur_key, count))
                cur_key = key
                count = 0
            count += 1
        if cur_key is not None:
            steps.append(_phase_from(cur_key, count))
        # Collapse to a readable number of phases.
        if len(steps) > _MAX_GRAPH_STEPS:
            steps = steps[: _MAX_GRAPH_STEPS - 1] + [
                {
                    "title": "Continued working",
                    "detail": f"{len(steps) - _MAX_GRAPH_STEPS + 1} further phases.",
                    "status": "ok",
                }
            ]
    else:
        steps = [
            {
                "title": "No trajectory recorded",
                "detail": "This trial has no ATIF trajectory to summarize.",
                "status": "warn",
            }
        ]
    _mark_terminal_phase(steps, outcome)
    return {
        "headline": f"{ctx.get('agent_name') or 'agent'} on {ctx.get('task_name') or 'task'}: {outcome}",
        "steps": steps,
        "terminal": {
            "last_action": _last_action(digest),
            "reason": _grounded_reason(ctx, outcome),
        },
        "source": "heuristic",
    }


def _phase_from(key: str, count: int) -> dict[str, Any]:
    label = {"reason": "Reasoning / planning"}.get(key, f"Used {key}")
    return {
        "title": label,
        "detail": f"{count} step{'s' if count != 1 else ''}.",
        "status": "ok",
    }


def _last_action(digest: list[dict[str, Any]]) -> str:
    for d in reversed(digest):
        if d["tools"]:
            return f"Called {d['tools'][0]}"
        if d["message"]:
            return _clip(d["message"], 120)
    return "No recorded action"


def _normalize_graph(
    raw: dict[str, Any], ctx: dict[str, Any], outcome: str, digest: list[dict[str, Any]]
) -> dict[str, Any]:
    """Coerce the LLM output into the stored shape + stamp the authoritative outcome."""
    steps_in = raw.get("steps") if isinstance(raw, dict) else None
    steps: list[dict[str, Any]] = []
    for i, s in enumerate(steps_in or []):
        if not isinstance(s, dict):
            continue
        status = s.get("status")
        if status not in ("ok", "warn", "error"):
            status = "ok"
        steps.append(
            {
                "id": f"s{i}",
                "title": _clip(s.get("title") or f"Step {i + 1}", 80),
                "detail": _clip(s.get("detail"), 240),
                "status": status,
            }
        )
    if not steps:
        return _finalize(_heuristic_graph(digest, ctx, outcome), ctx, outcome)

    # Same guarantee as the other paths: a failing run can't render all-green
    # phases. Respects an error phase the model already located.
    _mark_terminal_phase(steps, outcome)

    terminal_in = raw.get("terminal") if isinstance(raw, dict) else None
    terminal_in = terminal_in if isinstance(terminal_in, dict) else {}
    graph = {
        "headline": _clip(raw.get("headline"), 200)
        or f"{ctx.get('agent_name') or 'agent'}: {outcome}",
        "steps": steps,
        "terminal": {
            "last_action": _clip(terminal_in.get("last_action") or _last_action(digest), 160),
            "reason": _clip(terminal_in.get("reason"), 240) or _grounded_reason(ctx, outcome),
        },
    }
    return _finalize(graph, ctx, outcome)


def _finalize(graph: dict[str, Any], ctx: dict[str, Any], outcome: str) -> dict[str, Any]:
    graph["schema_version"] = GRAPH_SCHEMA_VERSION
    graph["terminal"]["outcome"] = outcome
    graph["source"] = graph.get("source", "llm")
    graph["model"] = ctx.get("model")
    graph["num_steps"] = ctx.get("num_steps")
    return graph


async def _run_llm(
    prompt: str, model: str, trial_id: str | None = None
) -> dict[str, Any] | None:
    from oddish.core.llm import complete

    try:
        result = await complete(
            handler="trajectory_graph",
            prompt=prompt
            + "\n\nReturn ONLY minified JSON matching this schema:\n"
            + json.dumps(_GRAPH_SCHEMA),
            model=model,
            max_tokens=2048,
            trial_id=trial_id,
        )
    except Exception as exc:
        logger.warning("trajectory-graph LLM call failed: %s", exc)
        return None

    text = result.text.strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.lstrip().lower().startswith("json"):
            text = text.lstrip()[4:]
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end == -1:
        return None
    try:
        return json.loads(text[start : end + 1])
    except json.JSONDecodeError as exc:
        logger.warning("trajectory-graph LLM returned invalid JSON: %s", exc)
        return None


# Highlight sentiment: a pivotal moment whose wording flags trouble marks its
# phase 'warn'. Deterministic keyword match (no extra LLM call).
_TROUBLE_WORDS = (
    "error",
    "fail",
    "failed",
    "failure",
    "revert",
    "wrong",
    "broke",
    "broken",
    "stuck",
    "retry",
    "retried",
    "exception",
    "traceback",
    "timeout",
    "cannot",
    "could not",
    "couldn't",
    "invalid",
    "gave up",
    "abandon",
)


# Drop benign negated mentions ("no error", "0 failures", "without exceptions")
# before scanning, so they don't flag a phase as a snag.
_NEG_TROUBLE_RE = re.compile(
    r"\b(?:no|not|without|zero|0|never)\s+"
    r"(?:more\s+)?(?:errors?|failures?|exceptions?|issues?|problems?)\b",
    re.IGNORECASE,
)


def _is_trouble(text: str) -> bool:
    low = _NEG_TROUBLE_RE.sub(" ", (text or "").lower())
    for w in _TROUBLE_WORDS:
        # Word-boundary match for alphabetic markers (so "error" doesn't fire on
        # "terror"); plain substring for phrases / symbols like "✗".
        if w.isalpha():
            if re.search(rf"\b{re.escape(w)}", low):
                return True
        elif w in low:
            return True
    return False


def _graph_from_summary(
    summary: dict[str, Any], ctx: dict[str, Any], outcome: str, digest: list[dict[str, Any]]
) -> dict[str, Any]:
    """Build the graph from the shipped ``trajectory_summary`` (phases + highlights).

    Reuses the summary's phase segmentation as the graph's general steps so the
    Agent Graph and the Summary tab stay consistent, then layers on the two
    things the summary lacks: a per-phase status and the terminal outcome node.
    """
    phases = summary.get("phases") if isinstance(summary, dict) else None
    if not isinstance(phases, list) or not phases:
        return _finalize(_heuristic_graph(digest, ctx, outcome), ctx, outcome)

    highlights = summary.get("highlights") or []
    trouble_steps: set[int] = set()
    for h in highlights:
        if isinstance(h, dict) and _is_trouble(f"{h.get('title', '')} {h.get('why', '')}"):
            sid = h.get("step_id")
            if isinstance(sid, int):
                trouble_steps.add(sid)

    steps: list[dict[str, Any]] = []
    for i, ph in enumerate(phases):
        if not isinstance(ph, dict):
            continue
        step_ids = [s for s in (ph.get("step_ids") or []) if isinstance(s, int)]
        status = "warn" if trouble_steps.intersection(step_ids) else "ok"
        steps.append(
            {
                "id": f"s{i}",
                "title": _clip(ph.get("label") or f"Phase {i + 1}", 80),
                "detail": _clip(ph.get("gist"), 240),
                "status": status,
            }
        )
    # Phases were present but every entry was unusable -> fall back rather than
    # emit a stepless "summary" graph.
    if not steps:
        return _finalize(_heuristic_graph(digest, ctx, outcome), ctx, outcome)
    _mark_terminal_phase(steps, outcome)

    # The terminal describes the agent's ACTUAL last action (final ATIF step),
    # not the last summary highlight — highlights are ordered by ascending
    # step_id and may omit the closing steps.
    last_action = ""
    if digest:
        la = _last_action(digest)
        if la and la != "No recorded action":
            last_action = la
    if not last_action:
        # No trajectory to read the final step from: use the highlight for the
        # LATEST step (max step_id), not highlights[-1] -- persisted highlights
        # keep the LLM's array order, which isn't guaranteed sorted by step_id.
        latest = max(
            (
                h
                for h in highlights
                if isinstance(h, dict) and isinstance(h.get("step_id"), int)
            ),
            key=lambda h: h["step_id"],
            default=None,
        )
        if latest:
            last_action = _clip(latest.get("title"), 120)
    if not last_action:
        last_action = "No recorded action"

    headline = _clip(summary.get("summary"), 200) or (
        f"{ctx.get('agent_name') or 'agent'} on {ctx.get('task_name') or 'task'}: {outcome}"
    )
    graph = {
        "headline": headline,
        "steps": steps,
        "terminal": {
            "last_action": last_action,
            "reason": _grounded_reason(ctx, outcome),
        },
        "source": "summary",
    }
    return _finalize(graph, ctx, outcome)


async def build_trajectory_graph(
    trajectory: dict[str, Any] | None,
    ctx: dict[str, Any],
    model: str | None = None,
    summary: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Summarize a trial's trajectory into a step graph.

    ``trajectory`` is the parsed ATIF dict (or None when the trial has no
    trajectory). ``ctx`` carries the authoritative trial fields:
    ``status, reward, error_message, task_name, agent_name, num_steps`` and,
    when available, the grounding context ``task_instruction`` (the goal) and
    ``verifier_output`` (the grader stdout) so phases are judged against the
    goal and the terminal names the failing check.
    ``summary`` is the shipped ``trajectory_summary`` dict when available -- its
    phases are reused as the graph's steps (no extra LLM call). Without it, a
    dedicated LLM pass (``model``) segments the run, and failing that a
    deterministic heuristic. Always returns a graph dict::

        {headline, steps:[{id,title,detail,status}], terminal:{outcome,last_action,reason},
         source:"summary"|"llm"|"heuristic", model, num_steps}
    """
    trajectory = trajectory or {}
    digest = _digest_steps(trajectory)
    ctx = dict(ctx)
    ctx.setdefault("num_steps", len(trajectory.get("steps") or []))
    ctx["model"] = model
    outcome = _infer_outcome(ctx)

    # Preferred: reuse the already-generated phase segmentation, even when the
    # ATIF trajectory didn't parse into steps here (a persisted summary still
    # describes the run) -- so a summarized trial never falls to "No trajectory".
    # A present-but-empty phases list routes here too: _graph_from_summary falls
    # back to the heuristic for it, which avoids a redundant second LLM pass.
    if summary and isinstance(summary.get("phases"), list):
        return _graph_from_summary(summary, ctx, outcome, digest)

    if not digest:
        return _finalize(_heuristic_graph(digest, ctx, outcome), ctx, outcome)

    if not model:
        return _finalize(_heuristic_graph(digest, ctx, outcome), ctx, outcome)

    prompt = _build_prompt(ctx, outcome, _transcript(digest))
    raw = await _run_llm(prompt, model, trial_id=ctx.get("trial_id"))
    if raw is None:
        graph = _heuristic_graph(digest, ctx, outcome)
        graph["source"] = "heuristic"
        return _finalize(graph, ctx, outcome)
    return _normalize_graph(raw, ctx, outcome, digest)
