"""Prompt text for CohortComparisonBlock.

Kept apart from the block logic so prompt edits do not touch parsing.
"""
from __future__ import annotations

from pathlib import Path

from oddish import analyze as _analyze

from api.services.blocks.analyzer.cohort.cohort_taxonomy import (
    CATEGORY_DEFINITIONS,
    DISCOVERY_CAP,
    BehaviorCategory,
)

# Vendor tokens that appear as a routing prefix on a stored model id.
# Stripping is a whitelist, not a split on ".": `gpt-5.4` and
# `claude-opus-4-8` carry dots of their own, and a generic split would render
# them as "4" and "8".
_VENDOR_PREFIXES = (
    "anthropic",
    "openai",
    "google",
    "meta",
    "mistral",
    "amazon",
    "cohere",
)


# Cross-region inference profile prefixes on stored Bedrock ids. Not just
# "global.": Opus 4.1 and Opus 4 have no global profile and are stored as
# "us.anthropic...". Mirrors oddish.config._BEDROCK_REGION_PREFIXES.
_REGION_PREFIXES = ("global.", "us.", "eu.", "apac.", "apn.")


def short_model_name(raw: str) -> str:
    """``global.anthropic.claude-opus-4-8`` -> ``claude-opus-4-8``.

    Lives here rather than in the block because BOTH readers need it: the
    prompt, which shows the model on each trial, and the chips built from
    ``_model_counts``. Two spellings of one id read as two models.
    """
    name = raw.split("/")[-1]
    for prefix in _REGION_PREFIXES:
        if name.startswith(prefix):
            name = name[len(prefix) :]
            break
    head, _, rest = name.partition(".")
    if rest and head in _VENDOR_PREFIXES:
        name = rest
    return name


PREAMBLE = (
    "You are comparing two cohorts of recorded agent runs on the same task: "
    "runs that succeeded for good reasons, and runs that failed for good "
    "reasons. A developer wants to know what the successful runs did "
    "differently."
)

# A task whose runs all failed (or all succeeded) has one cohort, and it is
# often the most interesting case there is. Say so plainly rather than leaving
# the model to infer it from an empty list -- an empty <cohort> block with a
# "compare the two" preamble above it invites inventing the missing side.
SINGLE_PREAMBLE = (
    "You are describing ONE cohort of recorded agent runs on the same task: "
    "{label} runs. There is no second cohort -- every classified run on this "
    "task version landed on this side. A developer wants to know what these "
    "runs did. Do NOT speculate about how a run on the other side would have "
    "behaved, and do not describe the absent side at all: put every "
    "observation in `{field}` and leave the other list empty."
)


def preamble(*, successful: list[dict], failing: list[dict]) -> str:
    """Which framing the run gets, decided by which cohorts actually exist."""
    if successful and failing:
        return PREAMBLE
    if successful:
        return SINGLE_PREAMBLE.format(label="successful", field="successful")
    return SINGLE_PREAMBLE.format(label="failing", field="failing")


def taxonomy_section() -> str:
    """The categories WITH definitions.

    The trajectory-summary prompt ships its labels as one bare comma-separated
    line and mislabels systematically as a result; do not repeat that here.
    """
    lines = ["<categories>"]
    for member in BehaviorCategory:
        lines.append(f"- {member.value}: {CATEGORY_DEFINITIONS[member]}")
    lines.append("</categories>")
    lines.append(
        f"Report at most {DISCOVERY_CAP} observations per side for "
        f"{BehaviorCategory.BEHAVIOR_DISCOVERY.value}."
    )
    return "\n".join(lines)


def _subagents_attr(delegation: dict | None) -> str:
    """How many subagents the run spawned, as a `<trial>` attribute.

    Three states, and collapsing any two of them produces a false finding:
    a counted number, `n/a` for an agent with no subagent tool, and the
    attribute omitted entirely for a summary written before the count existed.
    Rendering either of the last two as `0` invites the comparison to report
    that failing runs chose not to delegate.
    """
    if not isinstance(delegation, dict):
        return ""
    if not delegation.get("capable"):
        return ' subagents="n/a"'
    dispatches = delegation.get("dispatches")
    if not isinstance(dispatches, int):
        return ""
    return f' subagents="{dispatches}"'


def cohort_section(label: str, trials: list[dict]) -> str:
    """One cohort's trials, as component streams the model can cite."""
    lines = [f"<cohort name=\"{label}\">"]
    for t in trials:
        # Same shortener the chips use. Two spellings of one model id --
        # `global.anthropic.claude-opus-4-8` in the prose the model writes,
        # `claude-opus-4-8` on the chip beside it -- read as two models.
        model = short_model_name(t.get("model") or "") if t.get("model") else ""
        attrs = f' model="{model}"' if model else ""
        attrs += _subagents_attr(t.get("delegation"))
        lines.append(f'  <trial id="{t["trial_id"]}"{attrs}>')
        for c in t.get("components") or []:
            ids = c.get("step_ids") or []
            if not ids:
                continue
            rng = f"[{min(ids)}-{max(ids)}]"
            lines.append(
                f'    {c.get("trajectory_component")} {rng} '
                f'{(c.get("summary") or "").strip()}'
            )
        lines.append("  </trial>")
    lines.append("</cohort>")
    return "\n".join(lines)


def instructions_section(template: str) -> str:
    # str.replace, not .format: the template body contains JSON braces.
    return template.replace("{{taxonomy}}", taxonomy_section())


# Resolve via the oddish package's own location, matching
# summarize_trajectory.py:43. Do NOT walk parents[] from this file: the count
# is wrong (parents[5] is `backend/`, not the repo root) and it breaks the
# moment the module moves.
_PROMPT_PATH = (
    Path(_analyze.__file__).resolve().parent / "prompts" / "cohort_comparison.txt"
)


def load_cohort_prompt_template() -> str:
    return _PROMPT_PATH.read_text()
