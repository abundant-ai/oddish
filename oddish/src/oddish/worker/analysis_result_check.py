"""Shared validator for analysis-trial artifacts.

One contract, enforced twice. The in-sandbox verifier stages this file next
to the generated ``test.sh`` together with an ``expected.json`` and runs it
as a script, so an artifact that violates the contract fails the trial and
normal trial retries re-run the agent. The host-side importer calls
:func:`check_analysis_result` with the same expected payload before storing
anything, so a result that somehow slipped past the verifier is refused
whole rather than half-imported.

Sandbox constraint: the analysis image carries a bare python3, so this
module must import nothing outside the standard library. The allowed value
vocabularies are therefore NOT hardcoded here: the host derives them from
the real enums and models (``analysis_check_payload`` in
``oddish.workers.analysis_trials``) and passes them in via ``expected``, so
this file cannot drift from what the importer's parsers accept.

The checks are deliberately at least as strict as the importer: anything the
importer would reject (and previously dropped silently) must already fail
here, where failing still buys a retry.
"""

from __future__ import annotations

import json
import sys


def _missing(value: object) -> bool:
    return not isinstance(value, str) or not value.strip()


def _is_plain_int(value: object) -> bool:
    # bool is an int subclass; a boolean line number is not a line number.
    return type(value) is int


def _is_number(value: object) -> bool:
    return type(value) in (int, float)


def _check_action_item(item: object, expected: dict, where: str) -> list[str]:
    """One QA finding (an ActionItem). Used for audit ``items`` entries and
    for the ``action_items`` inside each QA classification."""
    if not isinstance(item, dict):
        return [f"{where} is not an object"]
    errors: list[str] = []
    if item.get("source") not in expected["sources"]:
        errors.append(f"{where}.source must be one of {expected['sources']}")
    if item.get("problem_type") not in expected["problem_types"]:
        errors.append(
            f"{where}.problem_type must be one of {expected['problem_types']}"
        )
    dimension = item.get("dimension")
    spelled = dimension.strip().lower() if isinstance(dimension, str) else None
    if (
        dimension not in expected["dimensions"]
        and spelled not in expected["dimension_spellings"]
    ):
        errors.append(f"{where}.dimension must be one of {expected['dimensions']}")
    # The importer accepts the prompt's own heading spelling for the tier
    # field ("severity"); mirror that here.
    tier = item.get("tier", item.get("severity"))
    if tier not in expected["tiers"]:
        errors.append(f"{where}.tier must be one of {expected['tiers']}")
    if _missing(item.get("file")):
        errors.append(f"{where}.file must be a non-empty string")
    line_start = item.get("line_start")
    line_end = item.get("line_end")
    for key, value in (("line_start", line_start), ("line_end", line_end)):
        if not _is_plain_int(value) or value < 1:
            errors.append(f"{where}.{key} must be a positive integer")
    if (
        _is_plain_int(line_start)
        and _is_plain_int(line_end)
        and line_start > 0
        and line_end < line_start
    ):
        errors.append(f"{where}.line_end must be greater than or equal to line_start")
    for key in ("title", "detail", "recommendation"):
        if _missing(item.get(key)):
            errors.append(f"{where}.{key} must be a non-empty string")
    # Optional fields still fail the importer's parser when wrong-typed, so
    # they must fail here first, where failing buys a retry.
    for key in ("id", "links_to", "exploit_evidence"):
        if key in item and item[key] is not None and not isinstance(item[key], str):
            errors.append(f"{where}.{key} must be a string or null")
    for key in ("exploited", "causal"):
        if key in item and not isinstance(item[key], bool):
            errors.append(f"{where}.{key} must be a boolean")
    return errors


def _check_exploitation(entry: object, where: str) -> list[str]:
    if not isinstance(entry, dict):
        return [f"{where} is not an object"]
    errors: list[str] = []
    if _missing(entry.get("links_to")):
        errors.append(f"{where}.links_to must be a non-empty string")
    if not isinstance(entry.get("exploited"), bool):
        errors.append(f"{where}.exploited must be a boolean")
    if "causal" in entry and not isinstance(entry["causal"], bool):
        errors.append(f"{where}.causal must be a boolean")
    evidence = entry.get("exploit_evidence")
    if evidence is not None and not isinstance(evidence, str):
        errors.append(f"{where}.exploit_evidence must be a string or null")
    if entry.get("exploited") is True and _missing(evidence):
        errors.append(f"{where}.exploit_evidence is required when exploited is true")
    if entry.get("causal") is True and entry.get("exploited") is not True:
        errors.append(f"{where}.causal cannot be true when exploited is false")
    return errors


def _check_trajectory_summary(
    summary: object,
    expected: dict,
    where: str,
    *,
    has_trajectory: bool | None = None,
) -> list[str]:
    if not isinstance(summary, dict):
        return [f"{where} is not an object"]
    errors: list[str] = []
    extra_summary_keys = sorted(set(summary) - {"summary", "highlights", "components"})
    if extra_summary_keys:
        errors.append(f"{where} has unknown fields: {extra_summary_keys}")
    if _missing(summary.get("summary")):
        errors.append(f"{where}.summary must be a non-empty string")
    highlights = summary.get("highlights")
    if not isinstance(highlights, list):
        errors.append(f"{where}.highlights must be a list")
    else:
        for index, highlight in enumerate(highlights):
            hw = f"{where}.highlights[{index}]"
            if not isinstance(highlight, dict):
                errors.append(f"{hw} is not an object")
                continue
            extra_highlight_keys = sorted(set(highlight) - {"step_id", "title", "why"})
            if extra_highlight_keys:
                errors.append(f"{hw} has unknown fields: {extra_highlight_keys}")
            if not _is_plain_int(highlight.get("step_id")):
                errors.append(f"{hw}.step_id must be an integer")
            for key in ("title", "why"):
                if _missing(highlight.get(key)):
                    errors.append(f"{hw}.{key} must be a non-empty string")
        highlight_ids = [
            highlight.get("step_id")
            for highlight in highlights
            if isinstance(highlight, dict) and _is_plain_int(highlight.get("step_id"))
        ]
        if highlight_ids != sorted(highlight_ids):
            errors.append(f"{where}.highlights must be ordered by step_id")
    components = summary.get("components")
    if not isinstance(components, list):
        errors.append(f"{where}.components must be a list")
        return errors
    if has_trajectory is False:
        if highlights:
            errors.append(f"{where}.highlights must be empty without a trajectory")
        if components:
            errors.append(f"{where}.components must be empty without a trajectory")
        return errors
    if not components:
        errors.append(f"{where}.components must be a non-empty list")
        return errors
    allowed_components = expected.get("trajectory_components")
    allowed_actions = expected.get("actions")
    allowed_purposes = expected.get("purposes")
    for index, component in enumerate(components):
        cw = f"{where}.components[{index}]"
        if not isinstance(component, dict):
            errors.append(f"{cw} is not an object")
            continue
        extra_component_keys = sorted(
            set(component)
            - {"step_ids", "trajectory_component", "action", "purpose", "summary"}
        )
        if extra_component_keys:
            errors.append(f"{cw} has unknown fields: {extra_component_keys}")
        step_ids = component.get("step_ids")
        if (
            not isinstance(step_ids, list)
            or not step_ids
            or not all(_is_plain_int(s) for s in step_ids)
        ):
            errors.append(f"{cw}.step_ids must be a non-empty list of integers")
        elif len(step_ids) != len(set(step_ids)):
            errors.append(f"{cw}.step_ids must not contain duplicates")
        for key in ("trajectory_component", "action", "purpose", "summary"):
            if _missing(component.get(key)):
                errors.append(f"{cw}.{key} must be a non-empty string")
        if (
            allowed_components
            and component.get("trajectory_component") not in allowed_components
        ):
            errors.append(
                f"{cw}.trajectory_component must be one of {allowed_components}"
            )
        if allowed_actions and component.get("action") not in allowed_actions:
            errors.append(f"{cw}.action must be one of {allowed_actions}")
        if allowed_purposes and component.get("purpose") not in allowed_purposes:
            errors.append(f"{cw}.purpose must be one of {allowed_purposes}")
    return errors


def _check_classification(
    analysis: object,
    expected: dict,
    where: str,
    *,
    trial_id: str | None,
    evidence: dict | None,
) -> list[str]:
    if not isinstance(analysis, dict):
        return [f"{where} is not an object"]
    errors: list[str] = []
    if analysis.get("classification") not in expected["classifications"]:
        errors.append(
            f"{where}.classification must be one of {expected['classifications']}"
        )
    for key in ("subtype", "evidence", "root_cause", "recommendation"):
        if _missing(analysis.get(key)):
            errors.append(f"{where}.{key} must be a non-empty string")
    written_trial_name = analysis.get("trial_name")
    if written_trial_name is not None and written_trial_name != trial_id:
        errors.append(f"{where}.trial_name must match {trial_id!r}")
    reward = analysis.get("reward")
    if reward is not None and not _is_number(reward):
        errors.append(f"{where}.reward must be a number or null")
    authoritative_reward = evidence.get("reward") if evidence is not None else None
    if evidence is not None and "reward" in analysis and reward != authoritative_reward:
        errors.append(
            f"{where}.reward must match the authoritative reward {authoritative_reward!r}"
        )
    classification = analysis.get("classification")
    if authoritative_reward == 1.0 and classification not in (
        "GOOD_SUCCESS",
        "BAD_SUCCESS",
    ):
        errors.append(f"{where}.classification must be a SUCCESS label for reward 1")
    if authoritative_reward == 0.0 and classification not in (
        "GOOD_FAILURE",
        "BAD_FAILURE",
        "HARNESS_ERROR",
    ):
        errors.append(
            f"{where}.classification must be a FAILURE or HARNESS_ERROR label for reward 0"
        )
    action_items = analysis.get("action_items")
    if not isinstance(action_items, list):
        errors.append(f"{where}.action_items must be a list")
    else:
        for index, item in enumerate(action_items):
            errors.extend(
                _check_action_item(item, expected, f"{where}.action_items[{index}]")
            )
        has_post_trial_must_fix = any(
            isinstance(item, dict)
            and item.get("tier", item.get("severity")) == expected["must_fix_tier"]
            for item in action_items
        )
        if analysis.get("classification") == "GOOD_FAILURE" and (
            has_post_trial_must_fix
            or bool(expected.get("pre_trial_must_fix_ids"))
        ):
            errors.append(
                f"{where}.classification cannot be GOOD_FAILURE when the task "
                "has a must-fix finding"
            )
    exploitation = analysis.get("exploitation")
    if not isinstance(exploitation, list):
        errors.append(f"{where}.exploitation must be a list")
    else:
        for index, entry in enumerate(exploitation):
            errors.extend(_check_exploitation(entry, f"{where}.exploitation[{index}]"))
        if "pre_trial_item_ids" in expected:
            wanted_links = list(expected.get("pre_trial_item_ids") or [])
            seen_links = [
                entry.get("links_to")
                for entry in exploitation
                if isinstance(entry, dict) and isinstance(entry.get("links_to"), str)
            ]
            duplicates = sorted(
                {link for link in seen_links if link and seen_links.count(link) > 1}
            )
            missing = sorted(set(wanted_links) - set(seen_links))
            unknown = sorted(set(seen_links) - set(wanted_links))
            if duplicates:
                errors.append(f"{where}.exploitation has duplicate links: {duplicates}")
            if missing:
                errors.append(
                    f"{where}.exploitation is missing pre-trial items: {missing}"
                )
            if unknown:
                errors.append(
                    f"{where}.exploitation links to unknown pre-trial items: {unknown}"
                )
    return errors


def _check_qa(data: dict, expected: dict) -> list[str]:
    errors: list[str] = []
    trials = data.get("trials")
    if not isinstance(trials, list):
        return ['"trials" must be a list']
    if "verdict" not in data:
        errors.append('"verdict" key is missing')

    evidence_by_id = {
        item.get("trial_id"): item
        for item in expected.get("trial_evidence") or []
        if isinstance(item, dict) and isinstance(item.get("trial_id"), str)
    }
    seen: list[str] = []
    for index, entry in enumerate(trials):
        where = f"trials[{index}]"
        if not isinstance(entry, dict):
            errors.append(f"{where} is not an object")
            continue
        trial_id = entry.get("trial_id")
        if _missing(trial_id):
            errors.append(f"{where}.trial_id must be a non-empty string")
        else:
            seen.append(trial_id)
        errors.extend(
            _check_classification(
                entry.get("analysis"),
                expected,
                f"{where}.analysis",
                trial_id=trial_id if isinstance(trial_id, str) else None,
                evidence=evidence_by_id.get(trial_id),
            )
        )
        errors.extend(
            _check_trajectory_summary(
                entry.get("trajectory_summary"),
                expected,
                f"{where}.trajectory_summary",
                has_trajectory=(
                    evidence_by_id[trial_id].get("has_trajectory")
                    if trial_id in evidence_by_id
                    else None
                ),
            )
        )

    # Exactly the requested set, each trial exactly once. This is the check
    # that turns a partial or padded artifact into a retry instead of a
    # silently incomplete import.
    wanted = list(expected.get("trial_ids") or [])
    if wanted:
        duplicates = sorted({t for t in seen if seen.count(t) > 1})
        missing = sorted(set(wanted) - set(seen))
        unknown = sorted(set(seen) - set(wanted))
        if duplicates:
            errors.append(f"duplicate trial entries: {duplicates}")
        if missing:
            errors.append(f"missing entries for requested trials: {missing}")
        if unknown:
            errors.append(f"entries for unrequested trials: {unknown}")

    verdict = data.get("verdict")
    if expected.get("verdict_expected"):
        if not isinstance(verdict, dict):
            errors.append('"verdict" must be an object: a verdict was requested')
        else:
            if verdict.get("verdict") not in expected["verdicts"]:
                errors.append(f"verdict.verdict must be one of {expected['verdicts']}")
            if verdict.get("confidence") not in expected["confidences"]:
                errors.append(
                    f"verdict.confidence must be one of {expected['confidences']}"
                )
            primary_issue = verdict.get("primary_issue")
            if primary_issue is not None and not isinstance(primary_issue, str):
                errors.append("verdict.primary_issue must be a string or null")
            recommendations = verdict.get("recommendations", [])
            if not isinstance(recommendations, list) or not all(
                isinstance(r, str) for r in recommendations
            ):
                errors.append("verdict.recommendations must be a list of strings")
            reasoning = verdict.get("reasoning")
            if reasoning is not None and not isinstance(reasoning, str):
                errors.append("verdict.reasoning must be a string or null")
    elif verdict is not None:
        errors.append('"verdict" must be null: no verdict was requested for this task')
    return errors


def _check_audit(data: dict, expected: dict) -> list[str]:
    items = data.get("items")
    if not isinstance(items, list):
        return ['"items" must be a list']
    errors: list[str] = []
    for index, item in enumerate(items):
        errors.extend(_check_action_item(item, expected, f"items[{index}]"))
    return errors


def _check_summarize(data: dict, expected: dict) -> list[str]:
    errors: list[str] = []
    target = expected.get("target_trial_id")
    written = data.get("target_trial_id")
    if _missing(written):
        errors.append('"target_trial_id" must be a non-empty string')
    elif target and written != target:
        # A summary attributed to the wrong trial is worse than no summary:
        # the importer would overwrite an unrelated trial's telemetry.
        errors.append(f'"target_trial_id" must be {target!r}')
    errors.extend(
        _check_trajectory_summary(
            data.get("trajectory_summary"), expected, "trajectory_summary"
        )
    )
    return errors


def check_analysis_result(data: object, expected: dict) -> list[str]:
    """Return every way ``data`` violates the analysis contract; [] is valid."""
    if not isinstance(data, dict):
        return ["the artifact is not a JSON object"]
    kind = expected.get("kind")
    if kind == "qa":
        return _check_qa(data, expected)
    if kind == "audit":
        return _check_audit(data, expected)
    if kind == "summarize":
        return _check_summarize(data, expected)
    return [f"unknown artifact kind {kind!r}"]


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if len(args) != 2:
        print(
            "usage: analysis_result_check.py <artifact.json> <expected.json>",
            file=sys.stderr,
        )
        return 2
    artifact_path, expected_path = args
    with open(expected_path) as handle:
        expected = json.load(handle)
    try:
        with open(artifact_path) as handle:
            data = json.load(handle)
    except Exception as exc:  # noqa: BLE001 - any unreadable artifact fails the trial
        print(f"the artifact is not valid JSON: {exc}", file=sys.stderr)
        return 1
    errors = check_analysis_result(data, expected)
    for error in errors[:50]:
        print(error, file=sys.stderr)
    if len(errors) > 50:
        print(f"... and {len(errors) - 50} more", file=sys.stderr)
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
