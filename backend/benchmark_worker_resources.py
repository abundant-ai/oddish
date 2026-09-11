"""Compare reservations on copied tasks and archived logs, without database writes.

Files stay in /tmp; source objects are only read. Run CPU-only first;
--lower-memory explicitly selects the second stage. The manifest is a JSON list
of source trials with id, model, largest_files[{key,bytes}], and optionally
normal=true, task_id, task_s3_key to replay the copied task with its oracle.
"""

import json
from pathlib import Path
import modal
from modal_app import image, runtime_secrets

app = modal.App("oddish-worker-resource-comparison")
secrets = [
    *runtime_secrets,
    modal.Secret.from_dict(
        {
            "ODDISH_DATABASE_URL": "postgresql+asyncpg://unused:unused@127.0.0.1:1/unused",
        }
    ),
]


async def compare(manifest: list[dict], cpu: float, memory_mb: int, run_tasks: bool):
    import asyncio
    import hashlib
    import resource
    import time
    from datetime import datetime, timedelta, timezone
    from tempfile import TemporaryDirectory
    from oddish.db.storage import get_storage_client
    from oddish.workers.agents.codex_stdout_trajectory import (
        convert_codex_stdout_jsonl_to_trajectory,
    )
    from oddish.workers.agents.claude_code import (
        convert_claude_code_stream_text_to_trajectory,
    )
    from oddish.costs.recorder import WorkerBillingSpec
    from oddish.costs.modal_cost import DEFAULT_RATES, estimate_span_cost, select_rates

    configuration = f"cpu{cpu:g}-mem{memory_mb}"
    fc_id = modal.current_function_call_id()
    print(f"configuration={configuration} modal_function_call_id={fc_id}")
    storage = get_storage_client()
    reports, gaps = [], []
    done = asyncio.Event()

    async def heartbeat():
        last = time.monotonic()
        while not done.is_set():
            await asyncio.sleep(0.1)
            now = time.monotonic()
            gaps.append(now - last)
            last = now

    ticker = asyncio.create_task(heartbeat())
    try:
        with TemporaryDirectory(prefix="worker-resources-") as folder:
            root = Path(folder)
            for source in manifest:
                for artifact in source.get("largest_files", []):
                    key = artifact["key"]
                    if not (
                        key.endswith("/agent/codex.txt")
                        or key.endswith("/agent/claude-code.txt")
                        or key.endswith("/agent/trajectory.json")
                        or key.endswith("/result.json")
                    ):
                        continue
                    data = await storage.download_bytes(key)
                    path = root / (
                        "log-" + hashlib.sha256(key.encode()).hexdigest()[:12]
                    )
                    path.write_bytes(data)
                    del data
                    started = time.monotonic()
                    gap_start = len(gaps)
                    await asyncio.sleep(0)
                    if key.endswith("/agent/codex.txt"):
                        trajectory = convert_codex_stdout_jsonl_to_trajectory(
                            path, agent_version="archived", model_name=source["model"]
                        )
                        if trajectory is None:
                            raise RuntimeError(f"No trajectory recovered from {key}")
                        parsed = trajectory.to_json_dict()
                    elif key.endswith("/agent/claude-code.txt"):
                        parsed = convert_claude_code_stream_text_to_trajectory(
                            path.read_text(), model_name=source["model"]
                        )
                        if parsed is None:
                            raise RuntimeError(f"No trajectory recovered from {key}")
                    else:
                        parsed = json.loads(path.read_bytes())
                    output = root / "readable.json"
                    output.write_text(json.dumps(parsed))
                    reread = json.loads(output.read_text())
                    assert isinstance(reread, dict)
                    duration = time.monotonic() - started
                    await asyncio.sleep(0.11)
                    now = datetime.now(timezone.utc)
                    spec = WorkerBillingSpec(
                        cpu, memory_mb, True, cpu_limit=17 if cpu == 0.6 else None
                    )
                    rates = select_rates(DEFAULT_RATES, "modal", "function", None, now)
                    cost = estimate_span_cost(
                        now, now + timedelta(seconds=duration), spec.resources(), rates
                    )
                    report = dict(
                        source_trial=source["id"],
                        artifact=key,
                        bytes=artifact["bytes"],
                        runtime_seconds=duration,
                        steps=len(parsed.get("steps", [])),
                        heartbeat_max_gap_seconds=max(gaps[gap_start:], default=0),
                        peak_rss_mib=resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
                        / 1024,
                        output_bytes=output.stat().st_size,
                        readable=True,
                        estimated_cost=str(cost.cost_usd),
                    )
                    print(json.dumps(report))
                    reports.append(report)
                    del parsed, reread
                    path.unlink()
                    output.unlink()
                if run_tasks and source.get("normal"):
                    from harbor.models.environment_type import EnvironmentType
                    from oddish.workers.harbor.runner import run_harbor_trial_async

                    task = root / source["task_id"]
                    await storage.download_task_directory(source["task_s3_key"], task)
                    gap_start = len(gaps)
                    started = time.monotonic()
                    outcome = await run_harbor_trial_async(
                        task, "oracle", root / "jobs", environment=EnvironmentType.MODAL
                    )
                    await asyncio.sleep(0.11)
                    result = (
                        json.loads(outcome.job_result_path.read_text())
                        if outcome.job_result_path
                        else None
                    )
                    report = dict(
                        task=source["task_id"],
                        agent="oracle",
                        runtime_seconds=time.monotonic() - started,
                        reward=outcome.reward,
                        error=outcome.error,
                        exception_type=outcome.exception_type,
                        readable_result=isinstance(result, dict),
                        heartbeat_max_gap_seconds=max(gaps[gap_start:], default=0),
                        peak_rss_mib=resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
                        / 1024,
                    )
                    print(json.dumps(report))
                    reports.append(report)
    finally:
        done.set()
        await ticker
    return dict(
        configuration=configuration, modal_function_call_id=fc_id, reports=reports
    )


@app.function(
    image=image,
    secrets=secrets,
    cpu=1,
    memory=3072,
    nonpreemptible=True,
    max_containers=1,
    timeout=3600,
)
async def baseline(manifest: list[dict], run_tasks: bool):
    return await compare(manifest, 1, 3072, run_tasks)


@app.function(
    image=image,
    secrets=secrets,
    cpu=(0.6, 17),
    memory=3072,
    nonpreemptible=True,
    max_containers=1,
    timeout=3600,
)
async def cpu_candidate(manifest: list[dict], run_tasks: bool):
    return await compare(manifest, 0.6, 3072, run_tasks)


@app.function(
    image=image,
    secrets=secrets,
    cpu=(0.6, 17),
    memory=1536,
    nonpreemptible=True,
    max_containers=1,
    timeout=3600,
)
async def memory_candidate(manifest: list[dict], run_tasks: bool):
    return await compare(manifest, 0.6, 1536, run_tasks)


@app.local_entrypoint()
def main(
    manifest: str, output: str, lower_memory: bool = False, run_tasks: bool = False
):
    sources = json.loads(Path(manifest).read_text())
    results = []
    for fn in [memory_candidate] if lower_memory else [baseline, cpu_candidate]:
        results.append(fn.remote(sources, run_tasks))
        Path(output).write_text(json.dumps(results, indent=2))
