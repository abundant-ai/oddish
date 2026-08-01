"""CI entrypoint: evaluate the prompts a PR changed and emit the PR comment.

Usage (from the repo root, with the ``claude`` CLI on PATH and an Anthropic
credential in the environment)::

    python -m oddish.evals.prompt_eval ci \
        --repo-root . \
        --base-sha "$(git merge-base origin/main HEAD)" \
        --out-dir /tmp/prompt-eval \
        --comment-file /tmp/prompt-eval/comment.md

Only prompts whose file changed between the merge-base and HEAD get a
baseline-vs-candidate comparison; a goldens-only change runs the current
prompt candidate-only. When nothing eval-able changed, no comment file is
written and the workflow skips posting.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import subprocess
import sys
from pathlib import Path

from .goldens import load_cases, load_config
from .kinds import PROMPT_KIND_SPECS, classify_changed_paths, goldens_dir_for
from .report import render_comment
from .runner import run_template


def _git(repo_root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=repo_root, check=True, capture_output=True, text=True
    ).stdout


def _git_show(repo_root: Path, sha: str, path: str) -> str | None:
    try:
        return _git(repo_root, "show", f"{sha}:{path}")
    except subprocess.CalledProcessError:
        return None  # file did not exist at the base commit


def _short(repo_root: Path, sha: str) -> str:
    return _git(repo_root, "rev-parse", "--short", sha).strip()


def build_reports(
    repo_root: Path, base_sha: str, *, dry_run: bool = False
) -> list[dict]:
    changed = _git(repo_root, "diff", "--name-only", base_sha, "HEAD").splitlines()
    changes = classify_changed_paths(changed)
    goldens_root = repo_root / "evals" / "prompt-goldens"
    config = load_config(goldens_root)
    base_short = _short(repo_root, base_sha)
    head_short = _short(repo_root, "HEAD")

    reports: list[dict] = []
    for kind in changes.affected_kinds():
        spec = PROMPT_KIND_SPECS[kind]
        report: dict = {
            "kind": kind,
            "prompt_path": spec.prompt_path,
            "base_sha": base_short,
            "head_sha": head_short,
            "agents": [{"name": a.name, "model": a.model} for a in config.agents],
            "n_trials": config.n_trials,
            "candidate": None,
            "baseline": None,
            "note": None,
        }
        if not spec.supported:
            if kind not in changes.prompt_changed:
                continue  # config/goldens sweep never surfaces unsupported kinds
            report["note"] = f"⚠ Not scored: {spec.unsupported_reason}."
            reports.append(report)
            continue

        cases = load_cases(goldens_root, kind, max_cases=config.max_cases_per_kind)
        if not cases:
            if kind not in changes.prompt_changed:
                continue
            report["note"] = (
                "⚠ This prompt changed but has no goldens. Add cases under "
                f"`{goldens_dir_for(kind)}/cases/` to get regression scores."
            )
            reports.append(report)
            continue

        head_template_path = repo_root / spec.prompt_path
        if not head_template_path.exists():
            report["note"] = "⚠ Prompt file deleted in this PR; nothing to evaluate."
            reports.append(report)
            continue
        head_template = head_template_path.read_text()
        base_template = _git_show(repo_root, base_sha, spec.prompt_path)

        if dry_run:
            report["note"] = "(dry run — no model calls made)"
            reports.append(report)
            continue

        report["candidate"] = asyncio.run(
            run_template(kind, head_template, cases, config)
        )
        if base_template is not None and base_template != head_template:
            report["baseline"] = asyncio.run(
                run_template(kind, base_template, cases, config)
            )
        elif base_template is None:
            report["note"] = "New prompt file — no base version to compare against."
        else:
            report["note"] = (
                "Prompt text unchanged in this PR (goldens/config change) — "
                "candidate-only run."
            )
        reports.append(report)
    return reports


def _cmd_ci(args: argparse.Namespace) -> int:
    repo_root = Path(args.repo_root).resolve()
    reports = build_reports(repo_root, args.base_sha, dry_run=args.dry_run)
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "results.json").write_text(json.dumps(reports, indent=2))
    if not reports:
        print("prompt-eval: no eval-able prompt changes detected; no comment written")
        return 0
    comment = render_comment(reports)
    Path(args.comment_file).write_text(comment)
    print(comment)

    if args.fail_on_regression:
        for report in reports:
            baseline, candidate = report.get("baseline"), report.get("candidate")
            if not baseline or not candidate:
                continue
            base_passed = sum(1 for r in baseline if r["passed"])
            head_passed = sum(1 for r in candidate if r["passed"])
            if head_passed < base_passed:
                print(
                    f"prompt-eval: regression in {report['kind']} "
                    f"({head_passed} < {base_passed} passing runs)",
                    file=sys.stderr,
                )
                return 1
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m oddish.evals.prompt_eval")
    sub = parser.add_subparsers(dest="command", required=True)

    ci = sub.add_parser("ci", help="evaluate prompts changed since --base-sha")
    ci.add_argument("--repo-root", default=".")
    ci.add_argument("--base-sha", required=True)
    ci.add_argument("--out-dir", default="/tmp/prompt-eval")
    ci.add_argument("--comment-file", default="/tmp/prompt-eval/comment.md")
    ci.add_argument("--dry-run", action="store_true")
    ci.add_argument("--fail-on-regression", action="store_true")
    ci.set_defaults(func=_cmd_ci)

    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
