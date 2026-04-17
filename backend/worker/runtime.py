import os

from rich.console import Console

from oddish.config import settings
from oddish.db import reconfigure_database_connections

console = Console()


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

    console.print(f"[dim]Harbor jobs: {settings.harbor_jobs_dir}[/dim]")
    console.print(f"[dim]Default environment: {settings.harbor_environment}[/dim]")
