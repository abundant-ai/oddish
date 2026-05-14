#!/usr/bin/env python3
"""Emit the ``ci.job`` root span that ties every wrapped step in this
GitHub Actions job into one Logfire trace.

``lf_step.py`` derives a W3C ``traceparent`` from
``GITHUB_RUN_ID + GITHUB_RUN_ATTEMPT + GITHUB_JOB`` and attaches it as
the remote parent of every step span. That gives the step spans a
shared ``trace_id`` and a shared ``parent_span_id`` — but the parent
span itself doesn't exist yet, which is what makes Logfire flag the
trace as "missing root" until this script runs.

This step (intended to run as the LAST step of each job with
``if: always()``) emits a single span with:

- ``trace_id`` and ``span_id`` matching exactly what ``lf_step.py``
  derives, courtesy of ``logfire.AdvancedOptions(id_generator=...)``;
- ``start_time`` taken from ``CI_JOB_TRACE_START_NANOS``, an env var
  written by the "Open CI trace" step at the top of the job;
- ``end_time`` set to now;
- ``job.status`` (success / failure / cancelled / skipped) passed in
  via ``--status "${{ job.status }}"``.

When that env var is missing (e.g. the open step never ran because
something blew up before it), we emit the span with a 1-second
synthetic duration so the trace at least has a root — better an
inaccurate parent bar than no parent at all.
"""

from __future__ import annotations

import argparse
import hashlib
import os
import sys
import time


def _derive_ids() -> tuple[int, int] | None:
    run_id = os.environ.get("GITHUB_RUN_ID")
    if not run_id:
        return None
    attempt = os.environ.get("GITHUB_RUN_ATTEMPT", "1")
    job = os.environ.get("GITHUB_JOB", "")
    seed = f"{run_id}/{attempt}/{job}".encode()
    digest = hashlib.sha256(seed).digest()
    trace_id = int.from_bytes(digest[:16], "big")
    span_id = int.from_bytes(digest[16:24], "big")
    return trace_id, span_id


def _resource_attributes() -> dict[str, str]:
    attrs: dict[str, str] = {
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
    pr = os.environ.get("PR_NUMBER") or os.environ.get("GITHUB_PR_NUMBER", "")
    if pr:
        attrs["oddish.pr"] = pr
        attrs["github.run_url"] = (
            f"https://github.com/{attrs['github.repository']}"
            f"/actions/runs/{attrs['github.run_id']}"
        )
    return {k: v for k, v in attrs.items() if v}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--status", default="success")
    args = parser.parse_args()

    if not os.environ.get("LOGFIRE_TOKEN"):
        return 0

    ids = _derive_ids()
    if ids is None:
        return 0
    trace_id, span_id = ids

    try:
        import logfire
        from logfire import AdvancedOptions
        from opentelemetry import trace as otel_trace
        from opentelemetry.sdk.trace import IdGenerator
        from opentelemetry.trace import Status, StatusCode
    except ImportError:
        return 0

    raw_start = os.environ.get("CI_JOB_TRACE_START_NANOS", "")
    if raw_start.isdigit():
        start_nanos = int(raw_start)
    else:
        # No open marker — happens when the open step never ran. Emit
        # a 1-second placeholder so the trace at least gets a root.
        start_nanos = time.time_ns() - 1_000_000_000

    end_nanos = time.time_ns()

    attrs = _resource_attributes()
    existing = os.environ.get("OTEL_RESOURCE_ATTRIBUTES", "")
    merged = ",".join(
        filter(None, [existing, *(f"{k}={v}" for k, v in attrs.items())])
    )
    if merged:
        os.environ["OTEL_RESOURCE_ATTRIBUTES"] = merged

    class _FixedIdGenerator(IdGenerator):
        def generate_trace_id(self) -> int:
            return trace_id

        def generate_span_id(self) -> int:
            return span_id

    try:
        logfire.configure(
            service_name="oddish-ci",
            service_version=attrs.get("github.sha") or None,
            environment="github-actions",
            send_to_logfire="if-token-present",
            console=False,
            advanced=AdvancedOptions(id_generator=_FixedIdGenerator()),
        )
    except Exception as exc:  # noqa: BLE001
        print(f"lf_job_close: logfire.configure failed: {exc}", file=sys.stderr)
        return 0

    try:
        tracer = otel_trace.get_tracer("oddish-ci.job")
        span = tracer.start_span("ci.job", start_time=start_nanos)
        try:
            span.set_attribute("job.status", args.status)
            span.set_attribute(
                "job.duration_seconds",
                max(0.0, (end_nanos - start_nanos) / 1_000_000_000),
            )
            if args.status in ("failure", "cancelled", "timed_out"):
                span.set_status(Status(StatusCode.ERROR, description=args.status))
            else:
                span.set_status(Status(StatusCode.OK))
        finally:
            span.end(end_time=end_nanos)
    finally:
        try:
            logfire.shutdown()
        except Exception:  # noqa: BLE001
            pass

    return 0


if __name__ == "__main__":
    sys.exit(main())
