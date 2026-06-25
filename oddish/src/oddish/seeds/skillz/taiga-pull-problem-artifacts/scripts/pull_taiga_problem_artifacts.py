#!/usr/bin/env python3
"""Export Taiga problem attempts, transcripts, and QA reports.

This script is intentionally stdlib-only so it can run from any Codex
workspace that has Taiga auth available.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter
from pathlib import Path
from typing import Any


DEFAULT_BASE_URL = "https://taiga.ant.dev"
DEFAULT_OAUTH_CLIENT_ID = (
    "REDACTED_CLIENT_ID"
)
DEFAULT_OAUTH_CLIENT_SECRET = "REDACTED_CLIENT_SECRET"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Export Taiga attempts, transcripts, and QA reports for a problem version."
    )
    parser.add_argument(
        "target",
        nargs="?",
        help="Taiga problem-version URL or problem_version_id.",
    )
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL)
    parser.add_argument("--environment-id")
    parser.add_argument("--problem-id")
    parser.add_argument("--problem-version-id")
    parser.add_argument("--out-dir", default=".")
    parser.add_argument("--target-only", action="store_true")
    parser.add_argument("--include-logs", action="store_true")
    parser.add_argument("--timeout", type=int, default=120)
    parser.add_argument("--retries", type=int, default=3)
    return parser.parse_args()


def decode_jwt_payload(token: str) -> dict[str, Any]:
    try:
        _header, payload, *_rest = token.split(".")
        payload += "=" * (-len(payload) % 4)
        return json.loads(base64.urlsafe_b64decode(payload.encode()).decode())
    except Exception:
        return {}


def refresh_token(refresh_token_value: str) -> dict[str, Any] | None:
    body = urllib.parse.urlencode(
        {
            "client_id": DEFAULT_OAUTH_CLIENT_ID,
            "client_secret": DEFAULT_OAUTH_CLIENT_SECRET,
            "refresh_token": refresh_token_value,
            "grant_type": "refresh_token",
        }
    ).encode()
    req = urllib.request.Request(
        "https://oauth2.googleapis.com/token",
        data=body,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            return json.loads(resp.read().decode())
    except Exception:
        return None


def write_secure(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    try:
        path.chmod(0o600)
    except OSError:
        pass


def load_token() -> str:
    env_token = os.environ.get("TAIGA_TOKEN", "").strip()
    if env_token:
        return env_token

    auth_path = Path.home() / ".config" / "taiga" / "auth.json"
    if not auth_path.exists():
        raise SystemExit(
            "No Taiga auth found. Set TAIGA_TOKEN or create ~/.config/taiga/auth.json."
        )

    auth = json.loads(auth_path.read_text(encoding="utf-8"))
    token = auth.get("id_token", "")
    expires_at = float(auth.get("expires_at") or 0)
    if token and time.time() < expires_at - 120:
        return token

    refresh_value = auth.get("refresh_token", "")
    if refresh_value:
        refreshed = refresh_token(refresh_value)
        if refreshed and refreshed.get("id_token"):
            auth["id_token"] = refreshed["id_token"]
            auth["refresh_token"] = refreshed.get("refresh_token") or refresh_value
            auth["expires_at"] = time.time() + int(refreshed.get("expires_in", 3600)) - 60
            write_secure(auth_path, json.dumps(auth, indent=2))
            return auth["id_token"]

    if token:
        return token

    raise SystemExit("Taiga auth file does not contain an id_token.")


def parse_target(args: argparse.Namespace) -> tuple[str | None, str | None, str | None, str | None]:
    source_url = None
    env_id = args.environment_id
    problem_id = args.problem_id
    pv_id = args.problem_version_id

    target = args.target
    if target:
        parsed = urllib.parse.urlparse(target)
        if parsed.scheme and parsed.netloc:
            source_url = target
            query = urllib.parse.parse_qs(parsed.query)
            env_id = env_id or first(query.get("environmentId"))
            problem_id = problem_id or first(query.get("problemId"))
            path_match = re.search(r"/problem-versions/([^/?#]+)", parsed.path)
            if path_match:
                pv_id = pv_id or urllib.parse.unquote(path_match.group(1))
        else:
            pv_id = pv_id or target

    return source_url, env_id, problem_id, pv_id


def first(values: list[str] | None) -> str | None:
    if not values:
        return None
    value = values[0].strip()
    return value or None


def safe_slug(value: str, max_len: int = 120) -> str:
    slug = re.sub(r"[^A-Za-z0-9_.-]+", "-", value).strip("-")
    return (slug or "taiga-problem")[:max_len]


class TaigaClient:
    def __init__(self, base_url: str, token: str, timeout: int, retries: int):
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.timeout = timeout
        self.retries = retries

    def get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        return self.request("GET", path, params=params)

    def request(
        self,
        method: str,
        path: str,
        params: dict[str, Any] | None = None,
        body: Any = None,
    ) -> Any:
        url = self.base_url + path
        if params:
            clean_params = {k: v for k, v in params.items() if v is not None}
            if clean_params:
                url += "?" + urllib.parse.urlencode(clean_params)
        data = json.dumps(body).encode() if body is not None else None
        headers = {
            "Authorization": "Bearer " + self.token,
            "Accept": "application/json",
            "User-Agent": "codex-taiga-artifact-export/1.0",
        }
        if body is not None:
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        last_error: Exception | None = None
        for attempt in range(max(1, self.retries)):
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                    text = resp.read().decode(errors="replace")
                    return json.loads(text)
            except urllib.error.HTTPError as exc:
                text = exc.read().decode(errors="replace")
                if exc.code < 500 or attempt == self.retries - 1:
                    raise RuntimeError(f"{method} {path} failed HTTP {exc.code}: {text[:2000]}")
                last_error = exc
            except Exception as exc:
                if attempt == self.retries - 1:
                    raise
                last_error = exc
            time.sleep(1.5 * (attempt + 1))
        raise RuntimeError(f"{method} {path} failed: {last_error}")


def get_problem_version(
    client: TaigaClient,
    env_id: str | None,
    pv_id: str,
) -> dict[str, Any]:
    if env_id:
        try:
            return client.get(f"/api/environments/{quote(env_id)}/problem-versions/{quote(pv_id)}")
        except Exception:
            pass
    return client.get(f"/api/problem-versions/{quote(pv_id)}")


def quote(value: str) -> str:
    return urllib.parse.quote(value, safe="")


def safe_fetch(client: TaigaClient, path: str, errors: dict[str, Any], label: str) -> Any:
    try:
        return client.get(path)
    except Exception as exc:
        errors[label] = str(exc)
        return None


def collect_run_ids(
    problem_runs: list[dict[str, Any]],
    attempts: list[dict[str, Any]],
    pv_id: str,
    target_only: bool,
) -> list[str]:
    run_ids: list[str] = []
    rows: list[dict[str, Any]] = []
    rows.extend(problem_runs)
    rows.extend(attempts)
    for row in rows:
        if target_only and row.get("problem_version_id") != pv_id:
            continue
        run_id = row.get("id")
        if run_id and run_id not in run_ids:
            run_ids.append(run_id)
    return run_ids


def fetch_qa_reports(
    client: TaigaClient,
    env_id: str,
    problem_id: str,
    pv_id: str,
) -> dict[str, Any]:
    errors: dict[str, Any] = {}
    qa_jobs = safe_fetch(
        client,
        f"/api/environments/{quote(env_id)}/problem-versions/{quote(pv_id)}/qa-jobs",
        errors,
        "qa_jobs",
    ) or []
    qa_items = safe_fetch(
        client,
        f"/api/environments/{quote(env_id)}/problem-versions/{quote(pv_id)}/qa-items",
        errors,
        "qa_items",
    )
    pv_item_feedback = safe_fetch(
        client,
        f"/api/environments/{quote(env_id)}/problem-versions/{quote(pv_id)}/qa-item-feedback",
        errors,
        "qa_item_feedback_for_problem_version",
    )

    job_records: list[dict[str, Any]] = []
    for job in qa_jobs:
        job_id = job.get("id")
        if not job_id:
            continue
        print(f"Fetching QA job {job_id} ({job.get('qa_type')})", flush=True)
        record: dict[str, Any] = {
            "id": job_id,
            "qa_type": job.get("qa_type"),
            "list_entry": job,
            "errors": {},
        }
        for key, suffix in [
            ("detail", ""),
            ("feedback", "/feedback"),
            ("item_feedback", "/item-feedback"),
            ("followups", "/followups"),
        ]:
            record[key] = safe_fetch(
                client,
                f"/api/qa-jobs/{quote(job_id)}{suffix}",
                record["errors"],
                key,
            )
        job_records.append(record)

    return {
        "exported_at": now_utc(),
        "base_url": client.base_url,
        "environment_id": env_id,
        "problem_id": problem_id,
        "problem_version_id": pv_id,
        "endpoints": {
            "qa_jobs": f"/api/environments/{env_id}/problem-versions/{pv_id}/qa-jobs",
            "qa_items": f"/api/environments/{env_id}/problem-versions/{pv_id}/qa-items",
            "qa_item_feedback_for_problem_version": (
                f"/api/environments/{env_id}/problem-versions/{pv_id}/qa-item-feedback"
            ),
            "qa_job_detail": "/api/qa-jobs/{qa_job_id}",
            "qa_job_feedback": "/api/qa-jobs/{qa_job_id}/feedback",
            "qa_job_item_feedback": "/api/qa-jobs/{qa_job_id}/item-feedback",
            "qa_job_followups": "/api/qa-jobs/{qa_job_id}/followups",
        },
        "qa_jobs": qa_jobs,
        "qa_items": qa_items,
        "qa_item_feedback_for_problem_version": pv_item_feedback,
        "qa_job_records": job_records,
        "summary": {
            "qa_job_count": len(qa_jobs),
            "qa_types": [job.get("qa_type") for job in qa_jobs],
            "qa_item_count": len(qa_items or []),
            "qa_jobs_with_errors": {
                rec["id"]: rec["errors"] for rec in job_records if rec.get("errors")
            },
            "top_level_errors": errors,
            "job_summaries": [
                {
                    "id": job.get("id"),
                    "qa_type": job.get("qa_type"),
                    "status": job.get("status"),
                    "summary": (job.get("result") or {}).get("summary"),
                    "finding_count": (job.get("result") or {}).get("finding_count"),
                }
                for job in qa_jobs
            ],
        },
    }


def now_utc() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def main() -> int:
    args = parse_args()
    source_url, env_id, problem_id, pv_id = parse_target(args)
    if not pv_id:
        raise SystemExit("Missing problem version ID. Pass a Taiga URL or --problem-version-id.")

    token = load_token()
    client = TaigaClient(args.base_url, token, args.timeout, args.retries)
    problem_version = get_problem_version(client, env_id, pv_id)
    env_id = env_id or problem_version.get("environment_id")
    problem_id = problem_id or problem_version.get("problem_id")
    if not env_id or not problem_id:
        raise SystemExit(
            "Could not infer environment_id and problem_id. Pass --environment-id and --problem-id."
        )

    print(f"Fetching attempts for problem {problem_id}", flush=True)
    attempts = client.get(f"/api/environments/{quote(env_id)}/problems/{quote(problem_id)}/attempts")
    problem_runs = client.get(
        f"/api/environments/{quote(env_id)}/problems/{quote(problem_id)}/problem-runs"
    )

    run_ids = collect_run_ids(problem_runs, attempts, pv_id, args.target_only)
    run_records: list[dict[str, Any]] = []
    for index, run_id in enumerate(run_ids, 1):
        print(f"[{index}/{len(run_ids)}] Fetching run {run_id}", flush=True)
        record: dict[str, Any] = {"id": run_id, "errors": {}}
        for key, path in [
            ("problem_run_detail", f"/api/problem-runs/{quote(run_id)}"),
            ("transcript", f"/api/transcript/{quote(run_id)}"),
            ("boot_steps", f"/api/transcript/boot-steps/{quote(run_id)}"),
        ]:
            record[key] = safe_fetch(client, path, record["errors"], key)
        if args.include_logs:
            for key, path in [
                ("environment_logs", f"/api/problem-runs/{quote(run_id)}/environment-logs"),
                ("sandbox_runtime_logs", f"/api/problem-runs/{quote(run_id)}/sandbox-runtime-logs"),
            ]:
                record[key] = safe_fetch(client, path, record["errors"], key)
        run_records.append(record)

    qa_reports = fetch_qa_reports(client, env_id, problem_id, pv_id)

    attempts_by_pv = dict(Counter(row.get("problem_version_id") for row in attempts))
    problem_runs_by_pv = dict(Counter(row.get("problem_version_id") for row in problem_runs))
    target_run_ids = [
        row.get("id") for row in problem_runs if row.get("problem_version_id") == pv_id
    ]
    target_attempt_ids = [
        row.get("id") for row in attempts if row.get("problem_version_id") == pv_id
    ]

    payload = {
        "exported_at": now_utc(),
        "source_url": source_url,
        "base_url": client.base_url,
        "environment_id": env_id,
        "problem_id": problem_id,
        "target_problem_version_id": pv_id,
        "notes": [
            "Taiga attempts and problem-runs endpoints may return problem-level rows; use summary target IDs for the linked problem version.",
            "Full transcript values come from /api/transcript/{problem_run_id} and are stored as JSON strings.",
            "No auth token or local credential material is included in this export.",
        ],
        "endpoints": {
            "problem_version": f"/api/environments/{env_id}/problem-versions/{pv_id}",
            "attempts": f"/api/environments/{env_id}/problems/{problem_id}/attempts",
            "problem_runs": f"/api/environments/{env_id}/problems/{problem_id}/problem-runs",
            "run_detail": "/api/problem-runs/{problem_run_id}",
            "transcript": "/api/transcript/{problem_run_id}",
            "boot_steps": "/api/transcript/boot-steps/{problem_run_id}",
        },
        "problem_version": problem_version,
        "attempts": attempts,
        "problem_runs": problem_runs,
        "runs": run_records,
        "qa_reports": qa_reports,
        "summary": {
            "attempts_count": len(attempts),
            "problem_runs_count": len(problem_runs),
            "unique_run_ids_count": len(run_ids),
            "problem_runs_by_problem_version_id": problem_runs_by_pv,
            "attempts_by_problem_version_id": attempts_by_pv,
            "target_problem_version_run_ids": target_run_ids,
            "target_problem_version_attempt_ids": target_attempt_ids,
            "transcript_char_counts": {
                rec["id"]: len(rec.get("transcript") or "") for rec in run_records
            },
            "runs_with_errors": {
                rec["id"]: rec["errors"] for rec in run_records if rec.get("errors")
            },
            "qa_report_count": qa_reports["summary"]["qa_job_count"],
            "qa_report_types": qa_reports["summary"]["qa_types"],
            "qa_reports_with_errors": qa_reports["summary"]["qa_jobs_with_errors"],
            "qa_top_level_errors": qa_reports["summary"]["top_level_errors"],
            "target_only": bool(args.target_only),
            "include_logs": bool(args.include_logs),
        },
    }

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    slug = safe_slug(problem_id)
    pv_short = safe_slug(pv_id[:8])
    combined_path = out_dir / f"taiga_{slug}_attempts_transcripts_with_qa_{pv_short}.json"
    qa_path = out_dir / f"taiga_{slug}_qa_reports_{pv_short}.json"
    combined_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    qa_path.write_text(json.dumps(qa_reports, ensure_ascii=False), encoding="utf-8")

    print()
    print(f"Wrote combined: {combined_path.resolve()} ({combined_path.stat().st_size} bytes)")
    print(f"Wrote QA only:  {qa_path.resolve()} ({qa_path.stat().st_size} bytes)")
    print(
        json.dumps(
            {
                "attempts_count": payload["summary"]["attempts_count"],
                "problem_runs_count": payload["summary"]["problem_runs_count"],
                "unique_run_ids_count": payload["summary"]["unique_run_ids_count"],
                "target_problem_version_run_ids": target_run_ids,
                "qa_report_count": payload["summary"]["qa_report_count"],
                "qa_report_types": payload["summary"]["qa_report_types"],
                "runs_with_errors": payload["summary"]["runs_with_errors"],
                "qa_reports_with_errors": payload["summary"]["qa_reports_with_errors"],
                "qa_top_level_errors": payload["summary"]["qa_top_level_errors"],
            },
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
