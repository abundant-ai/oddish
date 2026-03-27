import os

from rich.console import Console

from cloud_policy import get_default_cloud_environment
from modal_app import MODEL_CONCURRENCY_DEFAULT, VOLUME_MOUNT_PATH
from oddish.config import Settings, settings
from oddish.db import reconfigure_database_connections

console = Console()


async def configure_storage_paths() -> None:
    """Configure storage paths to use Modal Volume."""
    Settings.local_storage_dir = f"{VOLUME_MOUNT_PATH}/tasks"
    Settings.harbor_jobs_dir = f"{VOLUME_MOUNT_PATH}/harbor"
    Settings.harbor_environment = get_default_cloud_environment().value
    # Keep pools small: each worker processes one job.
    # Modal can burst many containers at once, so keep both SQLAlchemy and
    # asyncpg pools tiny to avoid exhausting Supabase connection limits.
    Settings.db_pool_min_size = 1
    Settings.db_pool_max_size = 2
    Settings.db_pool_size = 1
    Settings.db_pool_max_overflow = 0
    settings.asyncpg_pool_min_size = 0
    settings.asyncpg_pool_max_size = 1
    settings.default_model_concurrency = MODEL_CONCURRENCY_DEFAULT

    # Modal containers are frequently reused. Rebuild the DB clients here so the
    # smaller worker pool sizes actually take effect for this invocation.
    await reconfigure_database_connections()

    os.makedirs(Settings.local_storage_dir, exist_ok=True)
    os.makedirs(Settings.harbor_jobs_dir, exist_ok=True)

    console.print(f"[dim]Storage: {Settings.local_storage_dir}[/dim]")
    console.print(f"[dim]Harbor jobs: {Settings.harbor_jobs_dir}[/dim]")
    console.print(f"[dim]Default environment: {Settings.harbor_environment}[/dim]")
