import os
from pathlib import Path

import modal
from dotenv import dotenv_values


def _env_flag(name: str, default: bool) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _env_int(name: str, default: int) -> int:
    value = os.environ.get(name)
    if value is None:
        return default
    return int(value)


MODAL_APP_NAME = os.environ.get("MODAL_APP_NAME", "oddish")
MODAL_VOLUME_NAME = os.environ.get("MODAL_VOLUME_NAME", "oddish")
MODAL_SECRET_ENVIRONMENT = os.environ.get("MODAL_SECRET_ENVIRONMENT", "main")
RUNTIME_SECRET_NAME = "oddish-prod"
ENABLE_BACKGROUND_WORKERS = _env_flag("ODDISH_ENABLE_MODAL_WORKERS", True)
API_MIN_CONTAINERS = _env_int("ODDISH_MODAL_API_MIN_CONTAINERS", 1)
API_BUFFER_CONTAINERS = _env_int("ODDISH_MODAL_API_BUFFER_CONTAINERS", 2)
API_MAX_CONTAINERS = _env_int("ODDISH_MODAL_API_MAX_CONTAINERS", 8)
API_CONCURRENCY_TARGET = _env_int("ODDISH_MODAL_API_CONCURRENCY_TARGET", 4)
API_CONCURRENCY_MAX = _env_int("ODDISH_MODAL_API_CONCURRENCY_MAX", 8)
LOCAL_DOTENV_PATH = Path(__file__).with_name(".env")
LOCAL_DOTENV_VARS = {
    key: value
    for key, value in dotenv_values(LOCAL_DOTENV_PATH).items()
    if value is not None
}

app = modal.App(MODAL_APP_NAME)

# Create Modal Volumes for shared storage between functions
# the volume isn't really used for anything
volume = modal.Volume.from_name(MODAL_VOLUME_NAME, create_if_missing=True)
VOLUME_MOUNT_PATH = "/data"
WORKER_TASK_MOUNT_PATH = "/mnt/oddish-tasks"
WORKER_TASK_MOUNT_KEY_PREFIX = "tasks/"

# Worker configuration
POLL_INTERVAL_SECONDS = 120  # How often to check for new jobs
# Allow ~12 hour trials with small shutdown buffer.
WORKER_TIMEOUT_SECONDS = _env_int("ODDISH_MODAL_WORKER_TIMEOUT_SECONDS", 43200)
SHUTDOWN_TIMEOUT_SECONDS = _env_int("ODDISH_MODAL_WORKER_SHUTDOWN_TIMEOUT_SECONDS", 10)
WORKER_MIN_CONTAINERS = _env_int(
    "ODDISH_MODAL_WORKER_MIN_CONTAINERS", 1
)  # Keep one job worker warm to reduce cold starts
WORKER_BUFFER_CONTAINERS = _env_int(
    "ODDISH_MODAL_WORKER_BUFFER_CONTAINERS", 4
)  # Keep a few extra warm workers during active bursts.
WORKER_SCALEDOWN_WINDOW_SECONDS = _env_int(
    "ODDISH_MODAL_WORKER_SCALEDOWN_WINDOW_SECONDS", 300
)  # Keep idle workers warm for 5 minutes
WORKER_MAX_CONTAINERS = _env_int(
    "ODDISH_MODAL_WORKER_MAX_CONTAINERS",
    256,
)  # High global cap so several queue keys can scale, but still not unbounded.

# Max number of workers spawned per poll cycle (rate limiter)
MAX_WORKERS_PER_POLL = _env_int("ODDISH_MODAL_MAX_WORKERS_PER_POLL", 16)

# Always attach the production Modal secret. Local deploys can layer a backend
# `.env` file on top for developer-specific overrides.
runtime_secret = modal.Secret.from_name(
    RUNTIME_SECRET_NAME, environment_name=MODAL_SECRET_ENVIRONMENT
)
runtime_secrets = [runtime_secret]
if LOCAL_DOTENV_VARS:
    runtime_secrets.append(modal.Secret.from_dict(LOCAL_DOTENV_VARS))

# Queue-key concurrency default for Modal runtime.
# Example:
# ODDISH_MODEL_CONCURRENCY_OVERRIDES='{"openai/gpt-5.2": 64, "anthropic/claude-3.7-sonnet": 32}'
MODEL_CONCURRENCY_DEFAULT = _env_int("ODDISH_MODEL_CONCURRENCY_DEFAULT", 64)

ENV_VARS = {
    "UV_LINK_MODE": "copy",
    # Claude CLI refuses --dangerously-skip-permissions when running as root (Modal default).
    # Setting IS_SANDBOX=1 tells it we're in a sandboxed environment and bypasses this check.
    "IS_SANDBOX": "1",
    # Oddish cloud settings — configures pydantic-settings fields in
    # oddish.config.Settings via ODDISH_* env vars.  Per-function DB pool
    # sizes are set in the entry modules (endpoints.py, worker/functions.py).
    "ODDISH_LOCAL_STORAGE_DIR": f"{VOLUME_MOUNT_PATH}/tasks",
    "ODDISH_HARBOR_JOBS_DIR": f"{VOLUME_MOUNT_PATH}/harbor",
    "ODDISH_HARBOR_ENVIRONMENT": "modal",
    "ODDISH_AUTO_START_WORKERS": "false",
    "ODDISH_ASYNCPG_POOL_MIN_SIZE": "0",
    "ODDISH_ASYNCPG_POOL_MAX_SIZE": "1",
    "ODDISH_DEFAULT_MODEL_CONCURRENCY": str(MODEL_CONCURRENCY_DEFAULT),
}


def _lookup_env(name: str) -> str | None:
    return os.environ.get(name) or LOCAL_DOTENV_VARS.get(name)


def _build_worker_task_mount_secret() -> modal.Secret:
    """
    Reuse the existing runtime secret when possible.

    CloudBucketMount expects AWS-style credential names, so local deploys that only
    provide Oddish's ODDISH_S3_* vars still need a tiny remap for the mount.
    """
    aws_access_key = _lookup_env("AWS_ACCESS_KEY_ID")
    aws_secret_key = _lookup_env("AWS_SECRET_ACCESS_KEY")
    aws_region = _lookup_env("AWS_REGION")
    aws_session_token = _lookup_env("AWS_SESSION_TOKEN")
    if aws_access_key and aws_secret_key:
        payload = {
            "AWS_ACCESS_KEY_ID": aws_access_key,
            "AWS_SECRET_ACCESS_KEY": aws_secret_key,
        }
        if aws_region:
            payload["AWS_REGION"] = aws_region
        if aws_session_token:
            payload["AWS_SESSION_TOKEN"] = aws_session_token
        return modal.Secret.from_dict(payload)

    oddish_access_key = _lookup_env("ODDISH_S3_ACCESS_KEY")
    oddish_secret_key = _lookup_env("ODDISH_S3_SECRET_KEY")
    oddish_region = _lookup_env("ODDISH_S3_REGION")
    if oddish_access_key and oddish_secret_key:
        payload = {
            "AWS_ACCESS_KEY_ID": oddish_access_key,
            "AWS_SECRET_ACCESS_KEY": oddish_secret_key,
        }
        if oddish_region:
            payload["AWS_REGION"] = oddish_region
        if aws_session_token:
            payload["AWS_SESSION_TOKEN"] = aws_session_token
        return modal.Secret.from_dict(payload)

    return runtime_secret


def _build_worker_task_bucket_mount() -> modal.CloudBucketMount | None:
    """Create a read-only bucket mount for worker task inputs when possible."""
    bucket_name = _lookup_env("ODDISH_S3_BUCKET")
    endpoint_url = _lookup_env("ODDISH_S3_ENDPOINT_URL")

    # Keep this worker optimization AWS-native for now; custom S3 endpoints still
    # use the existing SDK download path.
    if endpoint_url or not bucket_name:
        return None

    return modal.CloudBucketMount(
        bucket_name=bucket_name,
        key_prefix=WORKER_TASK_MOUNT_KEY_PREFIX,
        secret=_build_worker_task_mount_secret(),
        read_only=True,
    )


worker_task_bucket_mount = _build_worker_task_bucket_mount()
api_volumes = {VOLUME_MOUNT_PATH: volume}
worker_volumes = dict(api_volumes)
if worker_task_bucket_mount is not None:
    worker_volumes[WORKER_TASK_MOUNT_PATH] = worker_task_bucket_mount

image = (
    modal.Image.debian_slim(python_version="3.12")
    .apt_install(
        "git",
        "curl",
    )
    # Install Claude Code for trial analysis jobs that shell out to `claude -p`.
    .run_commands(
        "curl -fsSL https://claude.ai/install.sh | bash",
        "ln -sf /root/.local/bin/claude /usr/local/bin/claude",
    )
    .pip_install("psycopg2-binary")
    .env(ENV_VARS)
    # Copy oddish source BEFORE uv_sync (required for local path dependency)
    # The pyproject.toml references "../oddish" which resolves to /oddish from /root
    .add_local_dir(
        local_path="../oddish",
        remote_path="/oddish",
        copy=True,
        ignore=[".venv/", ".git"],
    )
    # Use backend's pyproject.toml which includes oddish as a dependency
    .add_local_file(
        local_path="./pyproject.toml",
        remote_path="/root/pyproject.toml",
        copy=True,
    )
    # Install all dependencies (oddish from /oddish, others from PyPI)
    .uv_sync()
    # Add backend-specific Python modules
    .add_local_python_source(
        "api",
        "auth",
        "cloud_policy",
        "endpoints",
        "modal_app",
        "models",
        "worker",
        "integrations",
        copy=True,
    )
)
