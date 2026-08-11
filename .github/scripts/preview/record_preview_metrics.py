"""Preview telemetry: ownership identity and the schema-v1 timing artifact.

A preview deployment is owned by a *generation* derived from the PR number and
head SHA. This module is the single canonical definition of that identity and
of the timing contract used to accept or reject later performance work.

Subcommands:
  generation  Print the generation id for $PR_NUMBER + $HEAD_SHA. Used by
              compute_deployment_plan.sh so the detect-changes job can export
              it as the `preview_generation` output consumed by later fencing.
  identity    Append the per-job ownership block (PR, head SHA, generation,
              event action, component plan) to $GITHUB_STEP_SUMMARY.
  record      Final-gate only: query the Actions Jobs API for this run, merge
              the external identifiers and sub-phase timings threaded through
              job outputs, write the timing artifact to the given path, and
              append a Markdown timing table to $GITHUB_STEP_SUMMARY.

Only public/service identifiers and URLs are recorded -- never tokens,
passwords, or secret values. URLs are stripped of userinfo, query, and
fragment before they enter the artifact, and the identifier set is a fixed
allowlist: adding a value requires editing IDENTIFIER_ENV below.

Inputs (env vars, record): GITHUB_REPOSITORY, GITHUB_RUN_ID, GITHUB_RUN_ATTEMPT,
GH_TOKEN, PR_NUMBER, HEAD_SHA, EVENT_ACTION, DEPLOY_BACKEND, RUN_MIGRATIONS,
DEPLOY_FRONTEND, VERCEL_TIMINGS (JSON object of phase -> seconds, or ""),
GITHUB_STEP_SUMMARY, and every value in IDENTIFIER_ENV.
"""

import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from urllib.parse import urlsplit, urlunsplit

SCHEMA_VERSION = 1

# Jobs API display name -> workflow job key. Matched as exact strings; if you
# rename a job in pr-preview.yml, rename it here too.
JOB_KEYS = {
    "Detect preview changes": "detect-changes",
    "Prepare preview database": "prepare-preview-database",
    "Deploy preview backend": "deploy-preview-backend",
    "Update Vercel preview": "update-vercel-preview",
    "Require working preview": "require-working-preview",
    "Post preview links": "post-preview-links",
}

# Job key -> the jobs that must settle before it can start. Used to attribute
# the gap between a need completing and this job starting (runner queueing).
JOB_NEEDS = {
    "detect-changes": [],
    "prepare-preview-database": ["detect-changes"],
    "deploy-preview-backend": ["detect-changes", "prepare-preview-database"],
    "update-vercel-preview": [
        "detect-changes",
        "prepare-preview-database",
        "deploy-preview-backend",
    ],
    "require-working-preview": [
        "detect-changes",
        "prepare-preview-database",
        "deploy-preview-backend",
        "update-vercel-preview",
    ],
    "post-preview-links": [
        "detect-changes",
        "prepare-preview-database",
        "deploy-preview-backend",
        "update-vercel-preview",
    ],
}

# Step name -> aggregated infra phase. These repeat across jobs and are summed.
INFRA_STEP_PHASES = {
    "Initialize containers": "container_init_seconds",
    "Checkout repository": "checkout_seconds",
    "Sync backend dependencies": "dependency_sync_seconds",
}

# Owning job -> (phase key, step name). When the owning job is skipped the
# phase is reported as the string "skipped", never as a zero-duration success.
PRIMARY_PHASES = {
    "prepare-preview-database": ("database_seconds", "Prepare preview database"),
    "deploy-preview-backend": ("modal_deploy_seconds", "Deploy preview backend"),
    "update-vercel-preview": ("vercel_seconds", "Update Vercel preview"),
    "require-working-preview": ("gate_verify_seconds", "Verify preview deployment"),
}

# Artifact identifier key -> env var. Fixed allowlist; see module docstring.
IDENTIFIER_ENV = {
    "supabase_branch_id": "SUPABASE_BRANCH_ID",
    "supabase_branch_ref": "SUPABASE_BRANCH_REF",
    "modal_app_name": "MODAL_APP_NAME",
    "modal_api_url": "MODAL_API_URL",
    "vercel_deployment_id": "VERCEL_DEPLOYMENT_ID",
    "vercel_preview_url": "VERCEL_PREVIEW_URL",
    "vercel_alias_url": "VERCEL_ALIAS_URL",
    "github_deployment_id": "GITHUB_DEPLOYMENT_ID",
}

PLAN_ENV = {
    "deploy_backend": "DEPLOY_BACKEND",
    "run_migrations": "RUN_MIGRATIONS",
    "deploy_frontend": "DEPLOY_FRONTEND",
}

API_ERRORS = (subprocess.CalledProcessError, FileNotFoundError, json.JSONDecodeError)


def preview_generation(pr_number, head_sha):
    """Canonical preview generation: identifies one desired preview state."""
    return f"pr{pr_number}-{head_sha[:12]}"


def sanitize_value(value):
    """Strip credentials, query, and fragment from URLs; pass others through."""
    if "://" not in value:
        return value
    parts = urlsplit(value)
    netloc = parts.hostname or ""
    if parts.port:
        netloc = f"{netloc}:{parts.port}"
    return urlunsplit((parts.scheme, netloc, parts.path, "", ""))


def collect_identifiers(env):
    return {
        key: sanitize_value(env[var]) for key, var in IDENTIFIER_ENV.items() if env.get(var)
    }


def collect_plan(env):
    return {key: env.get(var, "") for key, var in PLAN_ENV.items()}


def parse_timings(raw):
    """Sub-phase timings emitted by a job as a JSON `timings` output.

    Malformed payloads degrade to no sub-phases; telemetry must never fail a
    deployment step that produced it.
    """
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    if not isinstance(data, dict):
        return {}
    return {
        key: float(value)
        for key, value in data.items()
        if key.endswith("_seconds") and isinstance(value, (int, float))
    }


def parse_seed_stats(raw):
    """Run-level seed report emitted by the prepare job as a `seed_stats` output.

    Malformed payloads degrade to no seed section; telemetry must never fail a
    deployment step that produced it.
    """
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}


def _parse_ts(value):
    if not value:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _seconds(start, end):
    if start is None or end is None:
        return None
    return round((end - start).total_seconds(), 1)


def gh_api(path):
    result = subprocess.run(
        ["gh", "api", "-H", "Accept: application/vnd.github+json", path],
        capture_output=True,
        text=True,
        check=True,
    )
    return json.loads(result.stdout)


def build_report(run, jobs, env, now=None):
    """Assemble the schema-v1 timing artifact from API payloads and env.

    `run` is the Actions run object, `jobs` the Jobs API job list. Pure apart
    from `now` (injectable for tests); performs no I/O.
    """
    now = now or datetime.now(timezone.utc)
    run_created = _parse_ts(run.get("created_at"))

    by_key = {}
    for job in jobs:
        key = JOB_KEYS.get(job.get("name"), job.get("name") or "unknown")
        by_key[key] = job

    # Timestamps first: a job's wait is measured from its needs' completion, so
    # every job's completed_at must be known regardless of API payload order.
    settled = {}
    for key, job in by_key.items():
        settled[key] = (
            _parse_ts(job.get("started_at")),
            _parse_ts(job.get("completed_at")),
            job.get("conclusion"),
        )

    jobs_section = {}
    for key, job in by_key.items():
        started, completed, conclusion = settled[key]
        entry = {
            "name": job.get("name"),
            "conclusion": conclusion or "in_progress",
            "started_at": job.get("started_at"),
            "completed_at": job.get("completed_at"),
            "duration_seconds": _seconds(started, completed),
        }
        if conclusion == "skipped":
            jobs_section[key] = {"name": entry["name"], "conclusion": "skipped"}
            continue
        base = run_created
        for need in JOB_NEEDS.get(key, []):
            need_completed = settled.get(need, (None, None, None))[1]
            if need_completed and (base is None or need_completed > base):
                base = need_completed
        entry["wait_seconds"] = _seconds(base, started)
        steps = {}
        for step in job.get("steps") or []:
            name = step.get("name")
            if not name:
                continue
            if step.get("conclusion") == "skipped":
                steps[name] = "skipped"
                continue
            duration = _seconds(_parse_ts(step.get("started_at")), _parse_ts(step.get("completed_at")))
            if duration is not None:
                steps[name] = duration
        entry["steps"] = steps
        jobs_section[key] = entry

    phases = {}
    detect = jobs_section.get("detect-changes")
    if detect and detect.get("wait_seconds") is not None:
        phases["queue_delay_seconds"] = detect["wait_seconds"]
    for step_name, phase in INFRA_STEP_PHASES.items():
        total = sum(
            entry["steps"].get(step_name, 0)
            for entry in jobs_section.values()
            if entry.get("steps") and entry["steps"].get(step_name) != "skipped"
        )
        if total:
            phases[phase] = round(total, 1)
    for job_key, (phase, step_name) in PRIMARY_PHASES.items():
        entry = jobs_section.get(job_key)
        if entry is None:
            continue
        if entry["conclusion"] == "skipped":
            phases[phase] = "skipped"
            continue
        duration = (entry.get("steps") or {}).get(step_name)
        if isinstance(duration, (int, float)):
            phases[phase] = duration
    phases.update(parse_timings(env.get("VERCEL_TIMINGS", "")))
    if run_created:
        phases["total_wall_seconds"] = _seconds(run_created, now)

    return {
        "schema_version": SCHEMA_VERSION,
        "recorded_at": now.isoformat(),
        "repository": env.get("GITHUB_REPOSITORY", ""),
        "run_id": int(env.get("GITHUB_RUN_ID", "0") or 0),
        "run_attempt": int(env.get("GITHUB_RUN_ATTEMPT", "0") or 0),
        "pr_number": int(env.get("PR_NUMBER", "0") or 0),
        "head_sha": env.get("HEAD_SHA", ""),
        "generation": preview_generation(env.get("PR_NUMBER", "?"), env.get("HEAD_SHA", "")),
        "event_action": env.get("EVENT_ACTION", ""),
        "plan": collect_plan(env),
        "identifiers": collect_identifiers(env),
        "jobs": jobs_section,
        "phases": phases,
        "seed": parse_seed_stats(env.get("SEED_STATS", "")),
    }


def _fmt_duration(value):
    if value == "skipped":
        return "skipped"
    if value < 60:
        return f"{value:.1f}s"
    return f"{int(value // 60)}m{int(value % 60):02d}s"


PHASE_LABELS = [
    ("queue_delay_seconds", "Queue delay"),
    ("container_init_seconds", "Container init (all jobs)"),
    ("checkout_seconds", "Checkout (all jobs)"),
    ("dependency_sync_seconds", "Dependency sync (all jobs)"),
    ("database_seconds", "Database prepare"),
    ("modal_deploy_seconds", "Modal deploy + readiness"),
    ("vercel_seconds", "Vercel update (total)"),
    ("vercel_configure_seconds", "Vercel configure"),
    ("vercel_build_wait_seconds", "Vercel build wait"),
    ("vercel_alias_seconds", "Vercel alias + ready"),
    ("gate_verify_seconds", "Gate verification"),
    ("total_wall_seconds", "Total wall time"),
]


def render_markdown(report):
    """Concise timing table; tolerates missing or skipped phases."""
    lines = [
        f"## Preview timing (`{report['generation']}`)",
        "",
        "| Phase | Duration |",
        "| --- | ---: |",
    ]
    for key, label in PHASE_LABELS:
        value = report["phases"].get(key)
        if value is None:
            continue
        lines.append(f"| {label} | {_fmt_duration(value)} |")
    identifiers = report.get("identifiers") or {}
    if identifiers:
        lines += ["", "### Recorded identifiers", ""]
        for key, value in identifiers.items():
            lines.append(f"- `{key}`: {value}")
    return "\n".join(lines) + "\n"


def render_identity(env):
    """Per-job ownership block for the job summary."""
    pr_number = env.get("PR_NUMBER", "?")
    head_sha = env.get("HEAD_SHA", "")
    plan = collect_plan(env)
    plan_text = " ".join(f"{key}=`{value or '?'}`" for key, value in plan.items())
    return (
        "## Preview identity\n\n"
        f"- PR: #{pr_number}\n"
        f"- Head SHA: `{head_sha}`\n"
        f"- Generation: `{preview_generation(pr_number, head_sha)}`\n"
        f"- Event: `{env.get('EVENT_ACTION', '?')}`\n"
        f"- Plan: {plan_text}\n"
    )


def cmd_record(out_path):
    env = os.environ
    repo = env["GITHUB_REPOSITORY"]
    run_id = env["GITHUB_RUN_ID"]
    run = gh_api(f"/repos/{repo}/actions/runs/{run_id}")
    jobs = gh_api(f"/repos/{repo}/actions/runs/{run_id}/jobs?per_page=100").get("jobs", [])
    report = build_report(run, jobs, env)
    with open(out_path, "w") as f:
        json.dump(report, f, indent=2)
        f.write("\n")
    summary = env.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a") as f:
            f.write(render_markdown(report))
    print(f"wrote {out_path}")


def main():
    command = sys.argv[1] if len(sys.argv) > 1 else ""
    if command == "generation":
        print(preview_generation(os.environ["PR_NUMBER"], os.environ["HEAD_SHA"]))
    elif command == "identity":
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a") as f:
            f.write(render_identity(os.environ))
    elif command == "record" and len(sys.argv) == 3:
        cmd_record(sys.argv[2])
    else:
        print(__doc__, file=sys.stderr)
        sys.exit(2)


if __name__ == "__main__":
    main()
