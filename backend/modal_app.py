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
ENABLE_BACKGROUND_WORKERS = _env_flag("ODDISH_ENABLE_MODAL_WORKERS", True)
API_MIN_CONTAINERS = _env_int("ODDISH_MODAL_API_MIN_CONTAINERS", 1)
API_BUFFER_CONTAINERS = _env_int("ODDISH_MODAL_API_BUFFER_CONTAINERS", 2)
API_MAX_CONTAINERS = _env_int("ODDISH_MODAL_API_MAX_CONTAINERS", 16)
API_CONCURRENCY_TARGET = _env_int("ODDISH_MODAL_API_CONCURRENCY_TARGET", 8)
API_CONCURRENCY_MAX = _env_int("ODDISH_MODAL_API_CONCURRENCY_MAX", 16)
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

# Worker configuration
POLL_INTERVAL_SECONDS = 60  # How often to check for new jobs
# Allow ~30 min trials with small shutdown buffer.
WORKER_TIMEOUT_SECONDS = _env_int("ODDISH_MODAL_WORKER_TIMEOUT_SECONDS", 18000)
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
runtime_secrets = [
    modal.Secret.from_name("oddish-prod", environment_name=MODAL_SECRET_ENVIRONMENT)
]
if LOCAL_DOTENV_VARS:
    runtime_secrets.append(modal.Secret.from_dict(LOCAL_DOTENV_VARS))

# Environment configuration for Modal functions
# Note: Storage paths and harbor_environment are ClassVars in oddish.config,
# so we need to patch them at runtime or configure via trial submission.
ENV_VARS = {
    "UV_LINK_MODE": "copy",
    # Claude CLI refuses --dangerously-skip-permissions when running as root (Modal default).
    # Setting IS_SANDBOX=1 tells it we're in a sandboxed environment and bypasses this check.
    "IS_SANDBOX": "1",
}

# Queue-key concurrency default for Modal runtime.
# Example:
# ODDISH_MODEL_CONCURRENCY_OVERRIDES='{"openai/gpt-5.2": 64, "anthropic/claude-3.7-sonnet": 32}'
MODEL_CONCURRENCY_DEFAULT = _env_int("ODDISH_MODEL_CONCURRENCY_DEFAULT", 64)

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
        "endpoints",
        "modal_app",
        "models",
        "worker",
        "integrations",
        copy=True,
    )
)
