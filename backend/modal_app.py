import modal
from dotenv import dotenv_values

app = modal.App("oddish")

# Create Modal Volumes for shared storage between functions
# the volume isn't really used for anything
volume = modal.Volume.from_name("oddish", create_if_missing=True)
VOLUME_MOUNT_PATH = "/data"

# Worker configuration
POLL_INTERVAL_SECONDS = 30  # How often to check for new jobs
# Allow ~30 min trials with small shutdown buffer.
WORKER_TIMEOUT_SECONDS = 18000  # 5 hours max per job worker
SHUTDOWN_TIMEOUT_SECONDS = 10  # How long to wait for graceful shutdown

# Max number of workers spawned per poll cycle (rate limiter)
MAX_WORKERS_PER_POLL = 32

# Environment configuration for Modal functions
# Note: Storage paths and harbor_environment are ClassVars in oddish.config,
# so we need to patch them at runtime or configure via trial submission.
ENV_VARS = {
    **dotenv_values(".env"),
    "UV_LINK_MODE": "copy",
    # Claude CLI refuses --dangerously-skip-permissions when running as root (Modal default).
    # Setting IS_SANDBOX=1 tells it we're in a sandboxed environment and bypasses this check.
    "IS_SANDBOX": "1",
}

# Queue-key concurrency default for Modal runtime.
# Example:
# ODDISH_MODEL_CONCURRENCY_OVERRIDES='{"openai/gpt-5.2": 64, "anthropic/claude-3.7-sonnet": 32}'
MODEL_CONCURRENCY_DEFAULT = 64

image = (
    modal.Image.debian_slim(python_version="3.12")
    .apt_install(
        "git",
        "curl",
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
        copy=True,
    )
)
