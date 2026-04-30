"""Classify trial errors so harness/infra failures don't pollute eval results.

Mirrors the rule the frontend already uses in ``getMatrixStatus``
(``frontend/src/lib/status-config.ts``): a trial whose run produced a
non-empty ``error_message`` — and whose error isn't an
``AgentTimeoutError`` that *still* produced a verifier reward — is a
harness error rather than a legitimate model failure.

Two questions, two functions:

* :func:`is_harness_error` — should this trial's reward count toward the
  experiment's success rate? Used by the reward aggregator and any code
  that wants to mirror the UI's ``Error`` vs ``Fail`` distinction.

* :func:`is_fatal_infra` — should we *stop retrying immediately and cancel
  sibling trials*?  This is a strict subset of harness errors: only the
  classes that almost never recover within the same sandbox / API key
  (auth, model-not-found, OOM, agent-CLI launcher crash). Transient
  errors like ``RateLimitError`` or ``APIConnectionError`` deliberately
  do *not* match — they should keep using the existing retry loop.

The patterns are intentionally string-based so they work whether the
exception originates in harbor, litellm, an LLM SDK, modal, or the
agent CLI's exit code — Harbor's ``ExceptionInfo.exception_type`` is
always the leaf class name, and ``error_message`` carries the agent
launcher's exit-code line. Adding a new fatal class is one line.
"""

from __future__ import annotations

import re

# Anything in here means "trial finished but the result is not a valid
# evaluation of the model." Mirrors getMatrixStatus's `harness-error`
# branch.
_AGENT_TIMEOUT_MARKERS = ("AgentTimeoutError", "Agent execution timed out")


def is_harness_error(
    error_message: str | None,
    *,
    has_reward: bool,
) -> bool:
    """Return True for trials that the matrix UI labels "Error".

    ``has_reward`` is whether the verifier produced a numeric reward.
    The agent-timeout-with-reward case is special-cased: harbor
    sometimes records an ``AgentTimeoutError`` *and* still runs the
    verifier successfully; we treat those as legitimate fails (reward=0
    is meaningful), exactly like the frontend does.
    """
    if not error_message:
        return False
    is_agent_timeout = any(m in error_message for m in _AGENT_TIMEOUT_MARKERS)
    if is_agent_timeout and has_reward:
        return False
    return True


# Exception type names (leaf class, matches Harbor's ExceptionInfo.exception_type)
# that indicate a configuration or environment problem. Retrying never
# fixes them; cascade-cancel siblings.
_FATAL_EXCEPTION_TYPES: frozenset[str] = frozenset(
    {
        # litellm / provider SDKs — credential / model-id problems
        "AuthenticationError",
        "PermissionDeniedError",
        "NotFoundError",
        "ContextWindowExceededError",  # task prompt > model window: not a retry
        # harbor-side LLM input problems
        "ContextLengthExceededError",
        "OutputLengthExceededError",
        # harbor-side install / config
        "MissingExtraError",
        # modal / sandbox setup (raised before agent ever runs)
        "ImagePullError",
        "SandboxCreationError",
    }
)

# Agent CLI exit codes that mean "the launcher itself died, the model
# didn't get a chance" — retrying in the same sandbox almost never helps.
# 137 = SIGKILL (OOM), 139 = SIGSEGV, 127 = command not found,
# 126 = permission denied. We deliberately exclude 1 (generic) because
# legitimate agent failures often exit 1.
_FATAL_EXIT_CODES: frozenset[int] = frozenset({126, 127, 137, 139})

_EXIT_CODE_RE = re.compile(r"\bexit\s+(\d+)\b", re.IGNORECASE)


def is_fatal_infra(
    error_message: str | None,
    *,
    exception_type: str | None = None,
) -> bool:
    """Return True if retrying the trial in-place won't recover.

    Two signals:

    * ``exception_type`` matches a known fatal class (litellm auth,
      missing model, harbor's pre-flight errors, modal sandbox setup
      failures).
    * ``error_message`` contains an ``exit <code>`` for a code in
      :data:`_FATAL_EXIT_CODES` — covers agent-CLI launcher crashes
      that surface as ``NonZeroAgentExitCodeError`` with the exit code
      embedded in the message.
    """
    if exception_type and exception_type in _FATAL_EXCEPTION_TYPES:
        return True

    if error_message:
        match = _EXIT_CODE_RE.search(error_message)
        if match and int(match.group(1)) in _FATAL_EXIT_CODES:
            return True

    return False


def classify(
    error_message: str | None,
    *,
    exception_type: str | None,
    has_reward: bool,
) -> str:
    """Convenience: return one of "ok", "agent_failure", "harness_error",
    "fatal_infra". Used by callers that want a single label.

    * ``"ok"`` — no error, reward present.
    * ``"agent_failure"`` — verifier scored the trial; reward=0 is a
      real signal. ``error_message`` may be set (e.g. agent-timeout
      that still produced a reward).
    * ``"harness_error"`` — UI's "Error" pill; doesn't count toward
      success rate.
    * ``"fatal_infra"`` — strict subset of harness_error; retrying or
      running siblings is wasteful, so cascade-cancel + notify.
    """
    if is_fatal_infra(error_message, exception_type=exception_type):
        return "fatal_infra"
    if is_harness_error(error_message, has_reward=has_reward):
        return "harness_error"
    if has_reward:
        return "ok" if not error_message else "agent_failure"
    return "agent_failure"
