"""Render prompt-eval results as the sticky PR comment body."""

from __future__ import annotations

from collections import defaultdict

COMMENT_MARKER = "<!-- oddish-prompt-eval -->"


def _pass_stats(records: list[dict]) -> dict[str, dict]:
    stats: dict[str, dict] = defaultdict(lambda: {"passed": 0, "total": 0, "errors": 0})
    for record in records:
        bucket = stats[record["agent"]]
        bucket["total"] += 1
        if record["passed"]:
            bucket["passed"] += 1
        if record.get("error"):
            bucket["errors"] += 1
    return stats


def _rate(bucket: dict | None) -> str:
    if not bucket or not bucket["total"]:
        return "—"
    pct = 100.0 * bucket["passed"] / bucket["total"]
    cell = f"{bucket['passed']}/{bucket['total']} ({pct:.0f}%)"
    if bucket["errors"]:
        cell += f" ⚠{bucket['errors']} err"
    return cell


def _delta(base: dict | None, head: dict | None) -> str:
    if not base or not base["total"] or not head or not head["total"]:
        return "—"
    diff = (
        100.0 * head["passed"] / head["total"] - 100.0 * base["passed"] / base["total"]
    )
    if abs(diff) < 0.5:
        return "±0"
    return f"{diff:+.0f}pp"


def _case_cell(records: list[dict]) -> str:
    if not records:
        return "—"
    passed = sum(1 for record in records if record["passed"])
    total = len(records)
    mark = "✅" if passed == total else ("❌" if passed == 0 else "➖")
    cell = f"{mark} {passed}/{total}" if total > 1 else mark
    failing = next((r for r in records if not r["passed"]), None)
    if failing:
        detail = failing["detail"].replace("|", "\\|").replace("\n", " ")
        if len(detail) > 120:
            detail = detail[:117] + "..."
        cell += f" <sub>{detail}</sub>"
    return cell


def _by_case_agent(records: list[dict] | None) -> dict[tuple[str, str], list[dict]]:
    grouped: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for record in records or []:
        grouped[(record["case"], record["agent"])].append(record)
    return grouped


def render_kind(report: dict) -> str:
    lines = [f"### `{report['kind']}` — `{report['prompt_path']}`", ""]
    if report.get("note"):
        lines += [report["note"], ""]
    candidate = report.get("candidate")
    if candidate is None:
        return "\n".join(lines).rstrip() + "\n"

    baseline = report.get("baseline")
    base_label = f"Base `{report.get('base_sha', '?')}`"
    head_label = f"Head `{report.get('head_sha', '?')}`"
    base_stats = _pass_stats(baseline) if baseline is not None else {}
    head_stats = _pass_stats(candidate)

    if baseline is not None:
        lines += [
            f"| Agent | {base_label} | {head_label} | Δ |",
            "|---|---|---|---|",
        ]
    else:
        lines += [f"| Agent | {head_label} |", "|---|---|"]
    for agent in report.get("agents", []):
        name = agent["name"]
        head_bucket = head_stats.get(name)
        if baseline is not None:
            base_bucket = base_stats.get(name)
            lines.append(
                f"| {name} (`{agent['model']}`) | {_rate(base_bucket)} | "
                f"{_rate(head_bucket)} | {_delta(base_bucket, head_bucket)} |"
            )
        else:
            lines.append(f"| {name} (`{agent['model']}`) | {_rate(head_bucket)} |")

    base_grouped = _by_case_agent(baseline)
    head_grouped = _by_case_agent(candidate)
    case_names = sorted({record["case"] for record in candidate})
    agent_names = [agent["name"] for agent in report.get("agents", [])]
    lines += ["", "<details><summary>Per-case results</summary>", ""]
    if baseline is not None:
        header = "| Case | Agent | Base | Head |"
        rule = "|---|---|---|---|"
    else:
        header = "| Case | Agent | Head |"
        rule = "|---|---|---|"
    lines += [header, rule]
    for case in case_names:
        for agent in agent_names:
            head_cell = _case_cell(head_grouped.get((case, agent), []))
            if baseline is not None:
                base_cell = _case_cell(base_grouped.get((case, agent), []))
                lines.append(f"| `{case}` | {agent} | {base_cell} | {head_cell} |")
            else:
                lines.append(f"| `{case}` | {agent} | {head_cell} |")
    lines += ["", "</details>", ""]
    return "\n".join(lines)


def render_comment(kind_reports: list[dict]) -> str:
    lines = [
        COMMENT_MARKER,
        "## Prompt eval",
        "",
        "Golden-based comparison of the QA prompts changed in this PR "
        "(base = merge-base version, head = this PR). "
        "Corpus and config: `evals/prompt-goldens/`.",
        "",
    ]
    for report in kind_reports:
        lines += [render_kind(report), ""]
    return "\n".join(lines).rstrip() + "\n"
