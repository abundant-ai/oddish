#!/usr/bin/env python3
"""Wrap a CI step in a Logfire span.

Usage:
    lf_step.py <step-name> -- <command> [args...]

Why this exists
---------------
GitHub's UI tells you WHICH step failed, but not what was happening
INSIDE the step at the moment it died. By wrapping each meaningful
step in a Logfire span that's emitted to a dedicated
``deployment.environment=github-actions`` bucket, we get:

- a "pending span" record sent as soon as the step starts (Logfire's
  SDK default) so the row is visible in the live view IMMEDIATELY,
  not only after the step finishes — and survives a hard kill;
- the exit code on the closed span, so a quick filter in Logfire
  groups every red step across every preview build;
- ``github.run_id`` / ``oddish.pr`` resource attributes so the trace
  is one click away from the PR that produced it.

If ``LOGFIRE_TOKEN`` is unset or the Logfire SDK isn't installed
yet (which is the case for the very first steps of a fresh runner
before ``uv sync`` lands the backend dependency), this script falls
back to a transparent ``subprocess.call`` so wrapping a step never
changes behaviour for forks / self-hosters / clean CI bootstraps.
"""

from __future__ import annotations

import os
import subprocess
import sys


def _passthrough(cmd: list[str]) -> int:
    return subprocess.call(cmd)


def _split_argv(argv: list[str]) -> tuple[str, list[str]] | None:
    if "--" not in argv:
        return None
    sep = argv.index("--")
    name = " ".join(argv[:sep]).strip()
    cmd = argv[sep + 1 :]
    if not name or not cmd:
        return None
    return name, cmd


def _resolve_pr() -> str:
    pr = os.environ.get("PR_NUMBER") or os.environ.get("GITHUB_PR_NUMBER")
    if pr:
        return pr
    ref = os.environ.get("GITHUB_REF", "")
    if ref.startswith("refs/pull/"):
        parts = ref.split("/")
        if len(parts) >= 3 and parts[2].isdigit():
            return parts[2]
    return ""


def _resource_attributes(step_name: str) -> dict[str, str]:
    attrs: dict[str, str] = {
        "step.name": step_name,
        "github.repository": os.environ.get("GITHUB_REPOSITORY", ""),
        "github.workflow": os.environ.get("GITHUB_WORKFLOW", ""),
        "github.job": os.environ.get("GITHUB_JOB", ""),
        "github.run_id": os.environ.get("GITHUB_RUN_ID", ""),
        "github.run_attempt": os.environ.get("GITHUB_RUN_ATTEMPT", ""),
        "github.run_number": os.environ.get("GITHUB_RUN_NUMBER", ""),
        "github.actor": os.environ.get("GITHUB_ACTOR", ""),
        "github.event_name": os.environ.get("GITHUB_EVENT_NAME", ""),
        "github.sha": os.environ.get("GITHUB_SHA", ""),
        "github.head_ref": os.environ.get("GITHUB_HEAD_REF", ""),
        "github.base_ref": os.environ.get("GITHUB_BASE_REF", ""),
        "github.ref": os.environ.get("GITHUB_REF", ""),
        "github.runner_os": os.environ.get("RUNNER_OS", ""),
    }
    pr = _resolve_pr()
    if pr:
        attrs["oddish.pr"] = pr
        attrs["github.run_url"] = (
            f"https://github.com/{attrs['github.repository']}"
            f"/actions/runs/{attrs['github.run_id']}"
        )
    return {k: v for k, v in attrs.items() if v}


def main() -> int:
    parsed = _split_argv(sys.argv[1:])
    if parsed is None:
        print("usage: lf_step.py <step name> -- <command...>", file=sys.stderr)
        return 2
    step_name, cmd = parsed

    if not os.environ.get("LOGFIRE_TOKEN"):
        return _passthrough(cmd)

    try:
        import logfire
    except ImportError:
        return _passthrough(cmd)

    attrs = _resource_attributes(step_name)

    try:
        # OTEL_RESOURCE_ATTRIBUTES is the portable hook for resource
        # attributes — Logfire merges it into the SDK resource. Set it
        # before `logfire.configure` so every span (including the
        # pending one Logfire emits at span-start) carries the GitHub
        # metadata, not just the closed span.
        existing = os.environ.get("OTEL_RESOURCE_ATTRIBUTES", "")
        merged = ",".join(
            filter(
                None,
                [existing, *(f"{k}={v}" for k, v in attrs.items())],
            )
        )
        os.environ["OTEL_RESOURCE_ATTRIBUTES"] = merged

        logfire.configure(
            service_name="oddish-ci",
            service_version=attrs.get("github.sha") or None,
            environment="github-actions",
            send_to_logfire="if-token-present",
            console=False,
        )
    except Exception as exc:  # noqa: BLE001 — observability must not break CI
        print(f"lf_step: logfire.configure failed: {exc}", file=sys.stderr)
        return _passthrough(cmd)

    try:
        with logfire.span(
            "ci.step {step_name}",
            step_name=step_name,
            cmd=" ".join(cmd),
        ) as span:
            try:
                result = subprocess.run(cmd)
            except FileNotFoundError as exc:
                span.record_exception(exc)
                logfire.error(
                    "lf_step: command not found: {cmd}",
                    cmd=" ".join(cmd),
                )
                return 127
            span.set_attribute("step.exit_code", result.returncode)
            if result.returncode != 0:
                logfire.error(
                    "lf_step: {step_name} exited with code {exit_code}",
                    step_name=step_name,
                    exit_code=result.returncode,
                )
            return result.returncode
    finally:
        # Flush BEFORE the process exits so the closed span lands in
        # Logfire even when the runner is short on shutdown time.
        try:
            logfire.shutdown()
        except Exception:  # noqa: BLE001
            pass


if __name__ == "__main__":
    sys.exit(main())
