"""Task-submission timing harness.

Opt-in and excluded from CI -- see ``conftest.py`` for the gating switches
(``ODDISH_PERF`` + ``ODDISH_API_URL`` + ``ODDISH_API_KEY``).

What it measures
----------------
For N synthetic real (non-symlink) Harbor task dirs, it drives the real
``oddish run`` submission wire-path against ``ODDISH_API_URL``::

    POST /tasks/upload/init  ->  PUT <presigned storage>  ->
    POST /tasks/upload/complete  ->  POST /tasks/sweep  ->  GET /tasks/{id}

and records, per phase and in aggregate:

* **throughput**  -- tasks/min over the whole run;
* **per-call latency** -- p50 / p95 / max for each phase;
* **5xx count** -- responses with ``500 <= status < 600`` on any phase;
* **client vs server** -- local archive+hash CPU (pure client) is timed
  separately, and for each HTTP call the server+transfer time
  (``response.elapsed``) is split from the connection/client overhead
  (wall-clock minus ``elapsed``), so the two are isolatable.

Probe-off invariant
-------------------
The sweep pins ``run_probe=false``. The hosted backend can still flip a user's
default ON (``backend/api/routers/tasks.py`` -> ``_apply_user_run_probe_default``),
which auto-enqueues a probe append and skews timings. The harness GETs each
task afterwards and asserts ``run_probe is False`` with zero ``is_probe``
trials, failing loudly if the default flipped.

Configuration (env)
-------------------
* ``ODDISH_PERF_TASKS``       -- N (default 30).
* ``ODDISH_PERF_CONCURRENCY`` -- submit pool size (default 1, matching the
  current ``TASK_UPLOAD_CONCURRENCY``; raise it to measure higher concurrency).
* ``ODDISH_PERF_AGENT``       -- sweep agent (default ``nop``; deterministic,
  needs no model, burns no credits).
* ``ODDISH_PERF_MODEL``       -- sweep model (required for non-nop/oracle agents).
"""

from __future__ import annotations

import os
import shutil
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, cast

import httpx
import pytest

from oddish.cli.api import archive_task_dir, compute_task_content_hash
from oddish.cli.config import get_api_key, get_api_url

# Generous per-call ceilings; uploads here are tiny so these only bound a hung
# target. The PUT mirrors the CLI's longer storage-upload timeout.
_HTTP_TIMEOUT = 120.0
_PUT_TIMEOUT = 600.0
_PREP_PHASE = "prep(client)"


@dataclass
class CallSample:
    """One timed step. ``status``/``elapsed_s`` are None for the client-only
    prep step and for transport errors."""

    phase: str
    wall_s: float
    elapsed_s: float | None = None
    status: int | None = None


@dataclass
class TaskOutcome:
    task_id: str | None = None
    submitted: bool = False
    run_probe: bool | None = None
    probe_trials: int | None = None
    total_trials: int | None = None
    samples: list[CallSample] = field(default_factory=list)
    error: str | None = None


def _percentile(values: list[float], q: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    pos = (len(ordered) - 1) * q
    lo = int(pos)
    hi = min(lo + 1, len(ordered) - 1)
    return ordered[lo] + (ordered[hi] - ordered[lo]) * (pos - lo)


def _timed(
    client: httpx.Client, method: str, url: str, phase: str, **kwargs: Any
) -> tuple[httpx.Response | None, CallSample]:
    start = time.perf_counter()
    try:
        response = client.request(method, url, **kwargs)
    except httpx.HTTPError:
        # Transport error (timeout, DNS, reset): no status/elapsed to record.
        return None, CallSample(phase=phase, wall_s=time.perf_counter() - start)
    wall = time.perf_counter() - start
    return response, CallSample(
        phase=phase,
        wall_s=wall,
        elapsed_s=response.elapsed.total_seconds(),
        status=response.status_code,
    )


def _submit_one_task(
    api_url: str,
    headers: dict[str, str],
    task_dir: Path,
    *,
    agent: str,
    model: str | None,
) -> TaskOutcome:
    """Run init -> PUT -> complete -> sweep -> status for a single task."""
    outcome = TaskOutcome()

    prep_start = time.perf_counter()
    content_hash = compute_task_content_hash(task_dir)
    tarball_path = archive_task_dir(task_dir)
    outcome.samples.append(
        CallSample(phase=_PREP_PHASE, wall_s=time.perf_counter() - prep_start)
    )

    try:
        with httpx.Client(timeout=_HTTP_TIMEOUT, headers=headers) as client:
            init_body = {"name": task_dir.name, "content_hash": content_hash}
            init_resp, sample = _timed(
                client, "POST", f"{api_url}/tasks/upload/init", "init", json=init_body
            )
            outcome.samples.append(sample)
            if init_resp is None or init_resp.status_code != 200:
                outcome.error = f"init failed: {sample.status}"
                return outcome

            init_payload = cast(dict, init_resp.json())
            task_id = init_payload.get("task_id")
            outcome.task_id = task_id

            if not init_payload.get("content_unchanged"):
                upload_url = init_payload.get("upload_url")
                if not isinstance(upload_url, str) or not upload_url:
                    outcome.error = "init returned no presigned upload_url"
                    return outcome
                put_headers = dict(init_payload.get("upload_headers") or {})
                put_headers.setdefault(
                    "Content-Length", str(tarball_path.stat().st_size)
                )
                with httpx.Client(
                    timeout=_PUT_TIMEOUT, follow_redirects=True
                ) as put_client:
                    with tarball_path.open("rb") as body:
                        put_resp, sample = _timed(
                            put_client,
                            "PUT",
                            upload_url,
                            "put",
                            headers=put_headers,
                            content=body,
                        )
                outcome.samples.append(sample)
                if put_resp is None or put_resp.status_code not in {200, 201, 204}:
                    outcome.error = f"storage PUT failed: {sample.status}"
                    return outcome

                complete_body = {
                    "task_id": task_id,
                    "name": init_payload["name"],
                    "version": init_payload["version"],
                    "content_hash": content_hash,
                }
                complete_resp, sample = _timed(
                    client,
                    "POST",
                    f"{api_url}/tasks/upload/complete",
                    "complete",
                    json=complete_body,
                )
                outcome.samples.append(sample)
                if complete_resp is None or complete_resp.status_code != 200:
                    outcome.error = f"complete failed: {sample.status}"
                    return outcome

            config: dict[str, Any] = {"agent": agent, "n_trials": 1}
            if model:
                config["model"] = model
            sweep_payload = {
                "task_id": task_id,
                "configs": [config],
                "run_analysis": False,
                # Pinned OFF: the auto-probe append path must not run (it would
                # add trials + DB round-trips and skew the timing). Asserted via
                # the GET below in case the backend flips the user default ON.
                "run_probe": False,
                "content_hash": content_hash,
            }
            sweep_resp, sample = _timed(
                client, "POST", f"{api_url}/tasks/sweep", "sweep", json=sweep_payload
            )
            outcome.samples.append(sample)
            if sweep_resp is None or sweep_resp.status_code != 200:
                outcome.error = f"sweep failed: {sample.status}"
                return outcome
            sweep_data = cast(dict, sweep_resp.json())
            outcome.task_id = sweep_data.get("id", task_id)
            outcome.submitted = True

            status_resp, sample = _timed(
                client, "GET", f"{api_url}/tasks/{outcome.task_id}", "status"
            )
            outcome.samples.append(sample)
            if status_resp is not None and status_resp.status_code == 200:
                status_data = cast(dict, status_resp.json())
                outcome.run_probe = bool(status_data.get("run_probe"))
                trials = status_data.get("trials") or []
                outcome.total_trials = len(trials)
                outcome.probe_trials = sum(1 for t in trials if t.get("is_probe"))
            else:
                # Couldn't read the task back -> the probe-off invariant is
                # unverifiable for it. Surface it instead of passing silently.
                outcome.error = f"status check failed: {sample.status}"
    finally:
        shutil.rmtree(Path(tarball_path).parent, ignore_errors=True)

    return outcome


def _format_report(
    outcomes: list[TaskOutcome], wall_total_s: float, *, concurrency: int
) -> tuple[str, dict[str, float]]:
    n = len(outcomes)
    submitted = sum(1 for o in outcomes if o.submitted)
    minutes = wall_total_s / 60.0
    tasks_per_min = (submitted / minutes) if minutes > 0 else 0.0

    by_phase: dict[str, list[CallSample]] = {}
    for outcome in outcomes:
        for sample in outcome.samples:
            by_phase.setdefault(sample.phase, []).append(sample)

    five_xx = sum(
        1
        for outcome in outcomes
        for sample in outcome.samples
        if sample.status is not None and 500 <= sample.status < 600
    )

    lines = [
        "",
        "=== task-submission timing harness ===",
        f"target            : {get_api_url()}",
        f"tasks (N)         : {n}   submitted ok: {submitted}   "
        f"concurrency: {concurrency}",
        f"wall total        : {wall_total_s:.2f}s",
        f"throughput        : {tasks_per_min:.1f} tasks/min",
        f"5xx responses     : {five_xx}",
        "per-phase latency (seconds):",
        f"  {'phase':<14}{'n':>4}  {'p50':>8}{'p95':>8}{'max':>8}"
        f"  {'srv+xfer p50':>13}{'conn p50':>10}",
    ]
    for phase in (_PREP_PHASE, "init", "put", "complete", "sweep", "status"):
        samples = by_phase.get(phase)
        if not samples:
            continue
        walls = [s.wall_s for s in samples]
        server = [s.elapsed_s for s in samples if s.elapsed_s is not None]
        conn = [s.wall_s - s.elapsed_s for s in samples if s.elapsed_s is not None]
        lines.append(
            f"  {phase:<14}{len(samples):>4}  "
            f"{_percentile(walls, 0.5):>8.3f}{_percentile(walls, 0.95):>8.3f}"
            f"{max(walls):>8.3f}  "
            f"{_percentile(server, 0.5):>13.3f}{_percentile(conn, 0.5):>10.3f}"
        )
    errors = [o.error for o in outcomes if o.error]
    if errors:
        lines.append(f"errors ({len(errors)}): {errors[:5]}")
    lines.append("=======================================")

    metrics = {
        "tasks": float(n),
        "submitted": float(submitted),
        "tasks_per_min": round(tasks_per_min, 3),
        "wall_total_s": round(wall_total_s, 3),
        "five_xx": float(five_xx),
    }
    return "\n".join(lines), metrics


@pytest.mark.perf
def test_submit_timing_harness(
    synthetic_task_dirs: list[Path],
    record_property: Any,
    capsys: pytest.CaptureFixture[str],
) -> None:
    api_url = get_api_url().rstrip("/")
    api_key = get_api_key()
    headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
    agent = os.environ.get("ODDISH_PERF_AGENT", "nop").strip() or "nop"
    model = os.environ.get("ODDISH_PERF_MODEL", "").strip() or None
    try:
        concurrency = max(1, int(os.environ.get("ODDISH_PERF_CONCURRENCY", "1") or "1"))
    except ValueError:
        concurrency = 1

    def run(task_dir: Path) -> TaskOutcome:
        return _submit_one_task(api_url, headers, task_dir, agent=agent, model=model)

    start = time.perf_counter()
    if concurrency == 1:
        outcomes = [run(task_dir) for task_dir in synthetic_task_dirs]
    else:
        with ThreadPoolExecutor(max_workers=concurrency) as pool:
            outcomes = list(pool.map(run, synthetic_task_dirs))
    wall_total_s = time.perf_counter() - start

    report, metrics = _format_report(outcomes, wall_total_s, concurrency=concurrency)
    with capsys.disabled():
        print(report)
    for key, value in metrics.items():
        record_property(f"perf_{key}", value)

    submitted = [o for o in outcomes if o.submitted]
    assert submitted, (
        "no task submitted successfully -- check ODDISH_API_URL / ODDISH_API_KEY "
        f"and the target's health. First errors: "
        f"{[o.error for o in outcomes if o.error][:5]}"
    )

    # Every submitted task must have a read-back run_probe state, else the
    # probe-off invariant below is silently unverifiable.
    unverified = [o.task_id for o in submitted if o.run_probe is None]
    assert not unverified, (
        f"could not verify run_probe for {len(unverified)} submitted task(s) "
        f"(status read-back failed): {unverified[:5]}"
    )

    # Probe-off invariant: the backend must not have flipped run_probe on.
    flipped = [o.task_id for o in submitted if o.run_probe]
    assert not flipped, (
        f"run_probe flipped ON server-side for {len(flipped)} task(s): {flipped[:5]}. "
        "The user's run_probe_default likely defaulted ON; use an API key whose "
        "run_probe_default=false so the harness measures the no-probe path."
    )
    with_probe_trials = [
        (o.task_id, o.probe_trials) for o in submitted if (o.probe_trials or 0) > 0
    ]
    assert not with_probe_trials, (
        f"unexpected probe trials despite run_probe=false: {with_probe_trials[:5]}"
    )
