"""One-off: dump the full audit trail for probe trials, inside Modal.

Stitches together the three records that document what happened to a probe
trial but live in separate places today:

  1. the trial lifecycle row     (status / harbor_stage / reward / error)
  2. its worker_jobs records      (scheduling + per-attempt execution errors)
  3. the actual S3 artifacts      (does claude-code.txt / test-stdout.txt exist?)

Use it to explain an "agent produced no output" probe: it shows whether the
trial errored before producing artifacts (trial-level failure) or ran fine but
its artifacts never reached storage / weren't found at analysis time.

Runs in the deployed worker image with the ``oddish-prod`` secret. Read-only.

Run from the ``backend/`` dir so modal_app's relative build paths resolve:

    cd backend
    modal run probe_audit_trail_modal.py                       # scan last 10 probe trials
    modal run probe_audit_trail_modal.py --limit 25            # scan last 25
    modal run probe_audit_trail_modal.py --empty-only          # only no-output trials
    modal run probe_audit_trail_modal.py --trial-id <id>       # one trial, full detail
"""

import modal

from modal_app import image, runtime_secrets

app = modal.App("probe-audit-trail-oneoff")


def _trunc(value, n: int = 600):
    """Truncate long text fields so the dump stays readable."""
    if value is None:
        return None
    s = str(value)
    return s if len(s) <= n else s[:n] + f"... [+{len(s) - n} chars]"


async def _trail_for_trial(session, storage, trial) -> dict:
    """Assemble the audit trail for a single trial row (no re-run)."""
    from sqlalchemy import select

    from oddish.db import WorkerJobModel
    from oddish.worker.probe_analysis import extract_probe_artifacts

    hc = trial.harbor_config or {}
    analysis = trial.analysis or {}

    # 1. Lifecycle row -----------------------------------------------------
    lifecycle = {
        "id": trial.id,
        "task_id": trial.task_id,
        "experiment_id": trial.experiment_id,
        "is_probe": trial.is_probe,
        "status": str(trial.status),
        "harbor_stage": trial.harbor_stage,
        "origin": str(trial.origin),
        "agent": trial.agent,
        "model": trial.model,
        "environment": trial.environment,
        "attempts": trial.attempts,
        "max_attempts": trial.max_attempts,
        "reward": trial.reward,
        "error_message": _trunc(trial.error_message, 2000),
        "last_heartbeat_error": _trunc(trial.last_heartbeat_error),
        "superseded_by_trial_id": trial.superseded_by_trial_id,
        "created_at": str(trial.created_at),
        "updated_at": str(trial.updated_at),
        "trial_s3_key": trial.trial_s3_key,
        "harbor_result_path": trial.harbor_result_path,
        "mode": hc.get("mode"),
        "has_extra_instructions": bool(hc.get("extra_instructions")),
        "analysis_status": str(trial.analysis_status),
        "analysis_error": _trunc(trial.analysis_error),
        "analysis_headline": analysis.get("headline"),
    }

    # 2. worker_jobs (scheduling + execution attempts) ---------------------
    jobs_rows = (
        (
            await session.execute(
                select(WorkerJobModel)
                .where(WorkerJobModel.subject_table == "trials")
                .where(WorkerJobModel.subject_id == trial.id)
                .order_by(WorkerJobModel.created_at.asc())
            )
        )
        .scalars()
        .all()
    )
    worker_jobs = [
        {
            "id": j.id,
            "kind": str(j.kind),
            "status": str(j.status),
            "attempts": j.attempts,
            "max_attempts": j.max_attempts,
            "created_at": str(j.created_at),
            "started_at": str(j.started_at),
            "claimed_at": str(j.claimed_at),
            "stale_reaped_at": str(j.stale_reaped_at),
            "current_worker_id": j.current_worker_id,
            "modal_function_call_id": j.modal_function_call_id,
            "heartbeat_failure_count": j.heartbeat_failure_count,
            "last_heartbeat_error": _trunc(j.last_heartbeat_error),
            "error_message": _trunc(j.error_message, 2000),
            "result_summary": j.result_summary,
        }
        for j in jobs_rows
    ]

    # 3. S3 artifacts actually present ------------------------------------
    prefix = trial.trial_s3_key or storage._trial_prefix(trial.id)
    artifact_report: dict = {"prefix": prefix}
    try:
        keys = await storage.list_keys(prefix)
        artifact_report["key_count"] = len(keys)
        artifact_report["keys"] = [k[len(prefix):].lstrip("/") for k in keys[:60]]
        artifact_report["has_claude_code_txt"] = any(
            k.endswith("claude-code.txt") for k in keys
        )
        artifact_report["has_test_stdout_txt"] = any(
            k.endswith("test-stdout.txt") for k in keys
        )
        artifact_report["has_result_json"] = any(
            k.endswith("result.json") for k in keys
        )
        artifact_report["has_exception_txt"] = any(
            k.endswith("exception.txt") for k in keys
        )
    except Exception as exc:
        artifact_report["list_error"] = f"{type(exc).__name__}: {exc}"

    # result.json (Harbor's own outcome, incl. exception_info) ------------
    try:
        result_json = await storage.get_trial_result_json(prefix)
        if isinstance(result_json, dict):
            artifact_report["result_json_keys"] = sorted(result_json)[:30]
            artifact_report["result_json_error"] = _trunc(
                result_json.get("error"), 2000
            )
            artifact_report["result_json_exception_info"] = _trunc(
                result_json.get("exception_info"), 2000
            )
    except Exception as exc:
        artifact_report["result_json_error"] = f"{type(exc).__name__}: {exc}"

    # 4. What the analyzer would actually have seen -----------------------
    extracted = {"transcript_messages": None, "verifier_stdout_len": None}
    try:
        from oddish.db.storage import resolve_trial_directory

        trial_dir, temp_dir, _ = await resolve_trial_directory(
            trial_id=trial.id,
            trial_s3_key=trial.trial_s3_key,
            trial_result_path=trial.harbor_result_path,
        )
        try:
            arts = extract_probe_artifacts(trial_dir)
            extracted["transcript_messages"] = len(arts.get("agent_messages") or [])
            vs = arts.get("verifier_stdout")
            extracted["verifier_stdout_len"] = len(vs) if vs else 0
        finally:
            if temp_dir is not None:
                import shutil

                shutil.rmtree(temp_dir, ignore_errors=True)
    except Exception as exc:
        extracted["resolve_error"] = f"{type(exc).__name__}: {exc}"

    # Verdict: where did it break? ----------------------------------------
    empty_transcript = (extracted.get("transcript_messages") or 0) == 0
    if empty_transcript:
        if trial.error_message or lifecycle["reward"] is None:
            verdict = (
                "TRIAL-LEVEL FAILURE: empty transcript + trial error/None reward. "
                "Agent/verifier never produced artifacts. Check error_message + "
                "result_json_exception_info + worker_jobs[].error_message."
            )
        elif not artifact_report.get("has_claude_code_txt", True):
            verdict = (
                "ARTIFACT MISSING IN S3: trial looks successful but claude-code.txt "
                "is absent from storage. Upload or path-resolution problem."
            )
        else:
            verdict = (
                "ARTIFACT PRESENT BUT UNPARSED: claude-code.txt exists yet parsed to "
                "0 messages. Extraction/format problem in _parse_agent_messages."
            )
    else:
        verdict = "OK: transcript has content; not an empty-output trial."

    return {
        "lifecycle": lifecycle,
        "worker_jobs": worker_jobs,
        "artifacts": artifact_report,
        "analyzer_input": extracted,
        "verdict": verdict,
    }


@app.function(image=image, secrets=runtime_secrets, timeout=900)
async def audit(trial_id: str | None, limit: int, empty_only: bool) -> str:
    import json

    from sqlalchemy import select

    from oddish.db import TrialModel, get_session, get_storage_client

    storage = get_storage_client()
    async with get_session() as session:
        if trial_id:
            trial = await session.get(TrialModel, trial_id)
            if not trial:
                return f"Trial {trial_id} not found."
            trials = [trial]
        else:
            trials = (
                (
                    await session.execute(
                        select(TrialModel)
                        .where(TrialModel.is_probe.is_(True))
                        .where(TrialModel.superseded_by_trial_id.is_(None))
                        .order_by(TrialModel.created_at.desc())
                        .limit(limit)
                    )
                )
                .scalars()
                .all()
            )
        if not trials:
            return "No probe trials found."

        trails = [await _trail_for_trial(session, storage, t) for t in trials]

    if empty_only:
        trails = [
            t
            for t in trails
            if (t["analyzer_input"].get("transcript_messages") or 0) == 0
        ]

    summary = [
        {
            "trial_id": t["lifecycle"]["id"],
            "status": t["lifecycle"]["status"],
            "reward": t["lifecycle"]["reward"],
            "transcript_messages": t["analyzer_input"].get("transcript_messages"),
            "verdict": t["verdict"].split(":")[0],
        }
        for t in trails
    ]

    return json.dumps(
        {"scanned": len(trails), "summary": summary, "trails": trails},
        indent=2,
        default=str,
    )


@app.local_entrypoint()
def main(trial_id: str = "", limit: int = 10, empty_only: bool = False) -> None:
    print(audit.remote(trial_id=trial_id or None, limit=limit, empty_only=empty_only))
