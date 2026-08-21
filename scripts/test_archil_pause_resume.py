# /// script
# requires-python = ">=3.12"
# dependencies = ["archil>=0.11.0", "httpx==0.28.1"]
# ///

import argparse
import json
import os
import subprocess
import tempfile
import time
from pathlib import Path
from urllib.parse import urlparse

import httpx
from archil import Archil

AGENTS = (
    ("claude-code", "anthropic/claude-opus-5"),
    ("codex", "openai/gpt-5.6-sol"),
    ("cursor-cli", "cursor/composer-1.5"),
    ("mini-swe-agent", "openai/gpt-5.5-pro"),
)
POLL_SECONDS = 5


def request(client: httpx.Client, method: str, path: str, **kwargs):
    for attempt in range(4):
        try:
            response = client.request(method, path, **kwargs)
            response.raise_for_status()
            return response.json()
        except httpx.TransportError:
            if attempt == 3:
                raise
            time.sleep(POLL_SECONDS)


def task_args(task: str) -> list[str]:
    if Path(task).exists():
        return [task]
    parsed = urlparse(task)
    task_id = parsed.path.rstrip("/").rsplit("/", 1)[-1] if parsed.scheme else task
    return ["--task", task_id]


def start(
    client: httpx.Client, task: str, api_url: str, timeout: float
) -> tuple[list[str], str, list[str]]:
    experiment_name = f"archil-quota-pause-{int(time.time())}"
    config = {
        "agents": [
            {"name": name, "model_name": model, "n_trials": 1} for name, model in AGENTS
        ]
    }
    with tempfile.NamedTemporaryFile(mode="w", suffix=".json") as file:
        json.dump(config, file)
        file.flush()
        result = subprocess.run(
            [
                str(Path("oddish/.venv/bin/oddish")),
                "run",
                *task_args(task),
                "--config",
                file.name,
                "--env",
                "archil",
                "--experiment",
                experiment_name,
                "--api",
                api_url,
                "--json",
                "--quiet",
                "--force",
            ],
            capture_output=True,
            text=True,
        )
    if result.returncode:
        raise RuntimeError(result.stderr or result.stdout)

    if result.stdout.strip():
        payload = json.loads(result.stdout)
        experiment_url = payload["experiment_url"]
        experiment_id = experiment_url.rstrip("/").rsplit("/", 1)[-1]
        task_ids = [task["id"] for task in payload["tasks"]]
    else:
        tasks = request(client, "GET", "/tasks")
        submitted = [
            task for task in tasks if task.get("experiment_name") == experiment_name
        ]
        if not submitted:
            raise RuntimeError(result.stderr or "Oddish returned no submission")
        experiment_id = submitted[0]["experiment_id"]
        task_ids = [task["id"] for task in submitted]
        experiment_url = experiment_id
    print(f"started: {experiment_url}", flush=True)

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        trials = request(client, "GET", f"/experiments/{experiment_id}/trials")
        detailed_trials = [
            request(client, "GET", f"/trials/{trial['id']}") for trial in trials
        ]
        sandbox_ids = list(
            dict.fromkeys(
                job["external_id"]
                for trial in detailed_trials
                for job in trial.get("jobs", [])
                if job.get("provider") == "archil" and job.get("external_id")
            )
        )
        if len(sandbox_ids) == len(AGENTS):
            return task_ids, experiment_id, sandbox_ids
        print(f"sandboxes: {len(sandbox_ids)}/{len(AGENTS)}", flush=True)
        time.sleep(POLL_SECONDS)
    raise TimeoutError("Timed out waiting for sandboxes to start")


def wait_for(
    archil: Archil, sandbox_ids: list[str], target: str, timeout: float
) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        statuses = {
            sandbox_id: archil.sandboxes.get(sandbox_id).status
            for sandbox_id in sandbox_ids
        }
        print(f"waiting for {target}: {statuses}", flush=True)
        if all(status == target for status in statuses.values()):
            return
        time.sleep(POLL_SECONDS)
    raise TimeoutError(f"Timed out waiting for {target}")


def cleanup(client: httpx.Client, task_ids: list[str], experiment_id: str) -> None:
    result = request(
        client,
        "POST",
        "/tasks/cancel",
        json={"task_ids": task_ids, "experiment_id": experiment_id},
    )
    print(f"cleaned up {result.get('trials_cancelled', 0)} trials", flush=True)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("task")
    parser.add_argument("--timeout", type=float, default=3_600)
    args = parser.parse_args()

    api_url = os.environ["ODDISH_API_URL"].rstrip("/")
    headers = {"Authorization": f"Bearer {os.environ['ODDISH_API_KEY']}"}
    archil_options = {
        "api_key": os.environ["ARCHIL_API_KEY"],
        "region": os.getenv("ARCHIL_REGION", "aws-us-east-1"),
        "timeout": 120,
    }

    with httpx.Client(base_url=api_url, headers=headers, timeout=60) as client:
        quota = request(client, "GET", "/quotas/me")
        print(f"quota limit: ${quota['limit_usd']:.2f}", flush=True)
        input(f"Press Enter to start {len(AGENTS)} trials...")

        task_ids, experiment_id, sandbox_ids = start(
            client, args.task, api_url, args.timeout
        )
        try:
            with Archil(**archil_options) as archil:
                wait_for(archil, sandbox_ids, "paused", args.timeout)
                print("paused; raise the quota now", flush=True)
                wait_for(archil, sandbox_ids, "running", args.timeout)
                print("PASS: both sandboxes resumed", flush=True)
        finally:
            cleanup(client, task_ids, experiment_id)


if __name__ == "__main__":
    main()
