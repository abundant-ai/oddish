import os
from pathlib import Path

from rich.console import Console

from oddish.config import settings
from oddish.core.prompt_seeds import seed_prompts
from oddish.db import get_session, reconfigure_database_connections
from oddish.workers.harbor.runner import log_local_storage_snapshot

console = Console()

# Where the oddish-gcp secret's inline JSON is written for ADC discovery.
_GCP_ADC_CREDENTIALS_PATH = "/root/gcp-sa.json"


def _materialize_gcp_adc_credentials() -> None:
    """Write the GKE service-account JSON to a file so ADC can find it.

    Google client libraries discover credentials only via a file path
    (GOOGLE_APPLICATION_CREDENTIALS), never from inline JSON, so the oddish-gcp
    Modal secret ships as GOOGLE_APPLICATION_CREDENTIALS_JSON and is written out
    here. A no-op when the secret is absent (deployments without GKE).
    """
    creds_json = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS_JSON")
    if not creds_json:
        return
    path = Path(_GCP_ADC_CREDENTIALS_PATH)
    if not path.exists():
        path.write_text(creds_json)
    os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = str(path)


async def _seed_analyzer_prompts() -> None:
    """Ensure the built-in analyzer prompts (pre_trial_qa, post_trial_qa)
    exist before this worker can pick up a QA job.

    Previously only ``oddish prompt seed`` created these rows, so a fresh
    deploy with ``pre_trial_enabled`` on 404s on every QA job.
    ``seed_prompts`` only inserts missing keys, so re-running it here on
    every container invocation is a cheap no-op once seeded -- and it
    self-heals if a row is ever deleted, unlike a one-shot migration.
    Best-effort: a seeding hiccup must not block the worker from picking up
    its job.
    """
    try:
        async with get_session() as session:
            created = await seed_prompts(session)
            await session.commit()
        if created:
            console.print(f"[dim]Seeded prompts: {', '.join(created)}[/dim]")
    except Exception as e:  # noqa: BLE001 - seeding must not block worker startup
        console.print(f"[yellow]Prompt seeding skipped: {e}[/yellow]")


async def configure_storage_paths() -> None:
    """Prepare storage directories and refresh DB connections for Modal workers.

    Settings (storage dirs, pool sizes, harbor environment, etc.) are loaded
    from ODDISH_* env vars baked into the Modal image — see modal_app.py
    ENV_VARS and worker/functions.py for details.

    We still call reconfigure_database_connections() because Modal reuses
    containers and we want fresh connection pools per invocation.
    """
    await reconfigure_database_connections()
    await _seed_analyzer_prompts()

    os.makedirs(settings.harbor_jobs_dir, exist_ok=True)
    _materialize_gcp_adc_credentials()

    console.print(f"[dim]Harbor jobs: {settings.harbor_jobs_dir}[/dim]")
    console.print(f"[dim]Default environment: {settings.harbor_environment}[/dim]")
    log_local_storage_snapshot(settings.harbor_jobs_dir)
