import os
from pathlib import Path

from rich.console import Console

from oddish.config import settings
from oddish.db import reconfigure_database_connections
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


async def configure_storage_paths() -> None:
    """Prepare storage directories and refresh DB connections for Modal workers.

    Settings (storage dirs, pool sizes, harbor environment, etc.) are loaded
    from ODDISH_* env vars baked into the Modal image — see modal_app.py
    ENV_VARS and worker/functions.py for details.

    We still call reconfigure_database_connections() because Modal reuses
    containers and we want fresh connection pools per invocation.
    """
    await reconfigure_database_connections()

    os.makedirs(settings.harbor_jobs_dir, exist_ok=True)
    _materialize_gcp_adc_credentials()

    console.print(f"[dim]Harbor jobs: {settings.harbor_jobs_dir}[/dim]")
    console.print(f"[dim]Default environment: {settings.harbor_environment}[/dim]")
    log_local_storage_snapshot(settings.harbor_jobs_dir)
