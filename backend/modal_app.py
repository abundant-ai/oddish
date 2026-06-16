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


def _env_float(name: str, default: float) -> float:
    value = os.environ.get(name)
    if value is None:
        return default
    return float(value)


MODAL_APP_NAME = os.environ.get("MODAL_APP_NAME", "oddish")
MODAL_SECRET_ENVIRONMENT = os.environ.get("MODAL_SECRET_ENVIRONMENT", "main")
RUNTIME_SECRET_NAME = "oddish-prod"
# Per-app webhook label so PR previews don't collide on the shared
# `{workspace}-{environment}--{label}.modal.run` subdomain. Production keeps
# the historical "api" label; previews derive a unique one from the app name.
API_WEBHOOK_LABEL = "api" if MODAL_APP_NAME == "oddish" else f"{MODAL_APP_NAME}-api"
ENABLE_BACKGROUND_WORKERS = _env_flag("ODDISH_ENABLE_MODAL_WORKERS", True)
API_MIN_CONTAINERS = _env_int("ODDISH_MODAL_API_MIN_CONTAINERS", 1)
API_BUFFER_CONTAINERS = _env_int("ODDISH_MODAL_API_BUFFER_CONTAINERS", 16)
API_MAX_CONTAINERS = _env_int("ODDISH_MODAL_API_MAX_CONTAINERS", 24)
API_CONCURRENCY_TARGET = _env_int("ODDISH_MODAL_API_CONCURRENCY_TARGET", 4)
API_CONCURRENCY_MAX = _env_int("ODDISH_MODAL_API_CONCURRENCY_MAX", 8)

# Per-function CPU/memory. ``cpu`` is a reservation floor (containers may burst
# above it when the host has spare capacity); ``memory`` is in MiB. These were
# previously unset on every function except the single-job worker, so the API
# in particular ran on Modal's tiny default fractional-core reservation -- with
# up to API_CONCURRENCY_MAX requests sharing one event loop, CPU-bound work
# (Pydantic serialization, SQLAlchemy hydration, JWT verify) contended badly
# under load and showed up as latency that looked like "slow DB". The API gets
# the most headroom since it is the most concurrent, latency-sensitive surface.
API_CPU = _env_float("ODDISH_MODAL_API_CPU", 2.0)
API_MEMORY_MB = _env_int("ODDISH_MODAL_API_MEMORY_MB", 4096)
LOCAL_DOTENV_PATH = Path(__file__).with_name(".env")
LOCAL_DOTENV_VARS = {
    key: value
    for key, value in dotenv_values(LOCAL_DOTENV_PATH).items()
    if value is not None
}

app = modal.App(MODAL_APP_NAME)

# No shared Modal Volume: each container uses its own ephemeral ``/tmp`` for
# Harbor scratch (see ``oddish.config.Settings.harbor_jobs_dir`` default of
# ``/tmp/harbor-jobs``). Sharing a Modal Volume between workers previously
# caused cross-container inode accumulation; per-container ``/tmp`` makes that
# class of leak structurally impossible.
WORKER_TASK_MOUNT_PATH = "/mnt/oddish-tasks"
WORKER_TASK_MOUNT_KEY_PREFIX = "tasks/"

# Worker configuration
POLL_INTERVAL_SECONDS = _env_int("ODDISH_MODAL_POLL_INTERVAL_SECONDS", 180)
# The dispatcher (poll_queue) now only discovers active queue keys and spawns
# job workers -- it no longer runs the heavy reconciliation sweep inline, so it
# stays well under this timeout. Kept comfortably above 60s so spawning a large
# batch via asyncio.gather can never be SIGKILLed mid-flight (a kill there used
# to leave orphaned 'idle in transaction' locks that deadlocked the next poll).
DISPATCHER_TIMEOUT_SECONDS = _env_int("ODDISH_MODAL_DISPATCHER_TIMEOUT_SECONDS", 120)
# Queue-state reconciliation (stale-heartbeat reap, stage advances, orphaned
# slot release, owner backfill) runs in its own scheduled function, decoupled
# from dispatch. It gets a generous timeout so it is never SIGKILLed
# mid-transaction; the interval is a little longer than the poll interval since
# the stale-heartbeat threshold is 15 minutes and reconciliation does not need
# to run as often as dispatch.
CLEANUP_INTERVAL_SECONDS = _env_int("ODDISH_MODAL_CLEANUP_INTERVAL_SECONDS", 240)
CLEANUP_TIMEOUT_SECONDS = _env_int("ODDISH_MODAL_CLEANUP_TIMEOUT_SECONDS", 600)
# Allow ~12 hour trials.
WORKER_TIMEOUT_SECONDS = _env_int("ODDISH_MODAL_WORKER_TIMEOUT_SECONDS", 43200)
WORKER_MIN_CONTAINERS = _env_int(
    "ODDISH_MODAL_WORKER_MIN_CONTAINERS", 1
)  # Keep one job worker warm to reduce cold starts
WORKER_BUFFER_CONTAINERS = _env_int(
    "ODDISH_MODAL_WORKER_BUFFER_CONTAINERS", 4
)  # Keep a few extra warm workers during active bursts.
WORKER_SCALEDOWN_WINDOW_SECONDS = _env_int(
    "ODDISH_MODAL_WORKER_SCALEDOWN_WINDOW_SECONDS", 300
)  # Keep idle workers warm for 5 minutes
# Global cap on concurrent worker containers. This is the real safety bound on
# DB client connections: each worker holds ~2 pooler client connections
# (1 SQLAlchemy + 1 asyncpg), so the worst case is roughly
# ``WORKER_MAX_CONTAINERS * 2 + API(16 * 8) + dispatcher/reconciler``. On the
# 2XL Supabase tier (1500 max pooler clients, 380 Postgres max_connections,
# transaction pool size 100) 512 workers -> ~512*2 + 128 = ~1152 clients, ~77%
# of the 1500 cap, leaving margin for spikes/reconnects and direct connections.
# Concurrent transaction *execution* is gated by the 100-backend pool, not this
# count -- worker DB transactions (claim / heartbeat) are short, so a large
# mostly-idle-on-DB fleet is fine.
WORKER_MAX_CONTAINERS = _env_int(
    "ODDISH_MODAL_WORKER_MAX_CONTAINERS",
    512,
)

# Mark single-job worker containers as non-preemptible so Modal does not
# interrupt long-running trials / analyses / verdicts mid-execution. Modal
# applies a 3x CPU+memory price multiplier when this is enabled
# (https://modal.com/docs/guide/preemption);
WORKER_NONPREEMPTIBLE = _env_flag("ODDISH_MODAL_WORKER_NONPREEMPTIBLE", True)
DISPATCHER_NONPREEMPTIBLE = _env_flag("ODDISH_MODAL_DISPATCHER_NONPREEMPTIBLE", True)

# Per-function CPU/memory floors (see API_CPU/API_MEMORY_MB note above).
# - Worker: keeps the historical 1 core / 3 GiB; Harbor scratch + log handling
#   is the heaviest non-API workload.
# - Dispatcher: lightweight (discover active keys + spawn); a modest floor is
#   plenty now that it no longer runs the reconciliation sweep inline.
# - Reconciler: DB-bound multi-pass sweep; give it a bit more memory than the
#   dispatcher for the larger result sets it materializes.
WORKER_CPU = _env_float("ODDISH_MODAL_WORKER_CPU", 1.0)
WORKER_MEMORY_MB = _env_int("ODDISH_MODAL_WORKER_MEMORY_MB", 3072)
DISPATCHER_CPU = _env_float("ODDISH_MODAL_DISPATCHER_CPU", 1.0)
DISPATCHER_MEMORY_MB = _env_int("ODDISH_MODAL_DISPATCHER_MEMORY_MB", 1024)
RECONCILER_CPU = _env_float("ODDISH_MODAL_RECONCILER_CPU", 1.0)
RECONCILER_MEMORY_MB = _env_int("ODDISH_MODAL_RECONCILER_MEMORY_MB", 2048)

# Max number of workers spawned per poll cycle (rate limiter, global across all
# queue_keys). This is the dominant throughput ceiling: long agent trials hold a
# slot for their full duration (often 10-30+ min), so the steady-state pool of
# running workers is roughly (spawns_per_poll * trial_duration / poll_interval).
# At 64/180s the global rate could not fill the per-model concurrency limits
# (which sum into the hundreds), leaving most models far below their caps. The
# per-queue_key ``queue_slots`` limits and ``WORKER_MAX_CONTAINERS`` remain the
# real safety bounds; this just stops the dispatcher from starving them.
#
# 192 ramps the fleet toward WORKER_MAX_CONTAINERS within ~3 polls. The
# per-poll spawn burst is also the per-poll claim burst (each spawned worker
# runs one claim query), but claims are short and the 2XL box (8 dedicated
# cores, transaction pool 100) absorbs ~192 concurrent short claims per
# 180s tick comfortably.
MAX_WORKERS_PER_POLL = _env_int("ODDISH_MODAL_MAX_WORKERS_PER_POLL", 192)

# Wall-clock budget for how long one worker container keeps claiming and running
# jobs on its held slot before exiting. Lets short jobs (analysis / verdict /
# nop-oracle, which finish well inside a single POLL_INTERVAL_SECONDS) batch
# many per container instead of running one job and leaving the slot idle until
# the next poll; long agent trials exceed it on their first job and so still run
# one-per-container. Must stay well under WORKER_TIMEOUT_SECONDS and the slot
# lease (WORKER_TIMEOUT_SECONDS + 30).
WORKER_BATCH_BUDGET_SECONDS = _env_int("ODDISH_MODAL_WORKER_BATCH_BUDGET_SECONDS", 300)

runtime_secret = modal.Secret.from_name(
    RUNTIME_SECRET_NAME, environment_name=MODAL_SECRET_ENVIRONMENT
)
runtime_secrets = [runtime_secret]

# AWS credentials for the sauron S3 mirror. Kept in a separate Modal
# secret so it can be rotated independently of oddish-prod. Set
# ODDISH_SAURON_AWS_SECRET_NAME to override the secret name, or to "" to
# skip loading entirely (e.g. for envs without AWS access).
SAURON_AWS_SECRET_NAME = os.environ.get(
    "ODDISH_SAURON_AWS_SECRET_NAME", "aws-credentials"
)
if SAURON_AWS_SECRET_NAME:
    runtime_secrets.append(
        modal.Secret.from_name(
            SAURON_AWS_SECRET_NAME, environment_name=MODAL_SECRET_ENVIRONMENT
        )
    )

# Optional bring-your-own-credentials secret, layered alongside oddish-prod so
# personal creds never go into the shared oddish-prod secret. Holds the
# subscription tokens (CS_CLAUDE_CODE_OAUTH_TOKEN / CS_CODEX_AUTH_JSON_B64) and
# is consumed only by the subscription auth route in harbor_runner. Set
# ODDISH_EXTRA_SECRET_NAME (e.g. cs-creds) at deploy to enable; unset = no-op,
# so this is inert for normal deploys. It is mounted ONLY on the trial worker
# (see worker_secrets below), not the API or dispatcher, to keep the personal
# tokens' blast radius minimal.
EXTRA_SECRET_NAME = os.environ.get("ODDISH_EXTRA_SECRET_NAME", "")

if LOCAL_DOTENV_VARS:
    runtime_secrets.append(modal.Secret.from_dict(LOCAL_DOTENV_VARS))
# Per-PR DB override created by the modal-preview workflow. Gating on
# MODAL_APP_NAME (baked into the image) keeps the secret list identical
# at deploy and container init.
if MODAL_APP_NAME.startswith("oddish-pr-"):
    runtime_secrets.append(
        modal.Secret.from_name(
            f"{MODAL_APP_NAME}-db",
            environment_name=os.environ.get("MODAL_ENVIRONMENT", "preview"),
        )
    )

# Trial-worker secret bundle = runtime_secrets plus the optional
# bring-your-own-credentials secret. The CS_* subscription tokens are consumed
# only by harbor_runner, so the extra secret is mounted ONLY on the trial worker
# (process_single_job), never on the API or dispatcher. With EXTRA_SECRET_NAME
# unset this is identical to runtime_secrets, so it is inert for normal deploys.
worker_secrets = list(runtime_secrets)
if EXTRA_SECRET_NAME:
    worker_secrets.append(
        modal.Secret.from_name(
            EXTRA_SECRET_NAME, environment_name=MODAL_SECRET_ENVIRONMENT
        )
    )

# Queue-key concurrency default for Modal runtime.
# Example:
# ODDISH_MODEL_CONCURRENCY_OVERRIDES='{"openai/gpt-5.2": 64, "anthropic/claude-3.7-sonnet": 32}'
MODEL_CONCURRENCY_DEFAULT = _env_int("ODDISH_DEFAULT_MODEL_CONCURRENCY", 48)
NOP_ORACLE_CONCURRENCY = _env_int("ODDISH_MODAL_NOP_ORACLE_CONCURRENCY", 48)
# Per-model queue-key concurrency overrides. Baked into the deploy so the
# repo is the source of truth; operators can still override the whole JSON
# via the ODDISH_MODEL_CONCURRENCY_OVERRIDES env var / secret.
MODEL_CONCURRENCY_OVERRIDES = os.environ.get(
    "ODDISH_MODEL_CONCURRENCY_OVERRIDES",
    '{"google/gemini-3.5-flash": 128, '
    '"global.anthropic.claude-haiku-4-5-20251001-v1:0": 128, '
    '"openai/gpt-5.4-mini": 128}',
)

ENV_VARS = {
    "UV_LINK_MODE": "copy",
    # Claude CLI refuses --dangerously-skip-permissions when running as root (Modal default).
    # Setting IS_SANDBOX=1 tells it we're in a sandboxed environment and bypasses this check.
    "IS_SANDBOX": "1",
    # Route Claude Code through AWS Bedrock. Oddish persists Claude trials with
    # their Bedrock model id, and this flag selects the matching runtime route.
    "CLAUDE_CODE_USE_BEDROCK": "1",
    # Baked into the image so the container sees the same identity the
    # deploy host did (the per-PR secret gate above depends on it).
    "MODAL_APP_NAME": MODAL_APP_NAME,
    "MODAL_ENVIRONMENT": os.environ.get("MODAL_ENVIRONMENT", "main"),
    # Baked so the bring-your-own-creds secret gate (runtime_secrets) evaluates
    # identically in the container and on the deploy host.
    "ODDISH_EXTRA_SECRET_NAME": EXTRA_SECRET_NAME,
    # Comma-separated agents whose trials use the personal-subscription auth
    # route (Claude Code OAuth / Codex auth.json) instead of Bedrock/Azure.
    # Lets the stored model id stay standard (e.g. claude-opus-4-8) while the
    # agent still authenticates via the bring-your-own-creds secret above.
    "ODDISH_SUBSCRIPTION_AGENTS": os.environ.get("ODDISH_SUBSCRIPTION_AGENTS", ""),
    # Concurrency cap for serialized subscription buckets (codex sub-solo/...).
    # Defaults to 1 (serialized) to protect a shared refresh-sensitive credential;
    # raise it for a run that finishes inside the token-validity window.
    "ODDISH_SUBSCRIPTION_QUEUE_CONCURRENCY": os.environ.get(
        "ODDISH_SUBSCRIPTION_QUEUE_CONCURRENCY", "1"
    ),
    # Oddish cloud settings — configures pydantic-settings fields in
    # oddish.config.Settings via ODDISH_* env vars.  Per-function DB pool
    # sizes are set in the entry modules (endpoints.py, worker/functions.py).
    "ODDISH_HARBOR_ENVIRONMENT": "modal",
    "ODDISH_AUTO_START_WORKERS": "false",
    "ODDISH_ASYNCPG_POOL_MIN_SIZE": "0",
    "ODDISH_ASYNCPG_POOL_MAX_SIZE": "1",
    "ODDISH_DEFAULT_MODEL_CONCURRENCY": str(MODEL_CONCURRENCY_DEFAULT),
    "ODDISH_MODEL_CONCURRENCY_OVERRIDES": MODEL_CONCURRENCY_OVERRIDES,
    # nop/oracle do not call model providers; this cap is for Modal/DB/S3
    # pressure rather than provider rate limits.
    "ODDISH_NOP_ORACLE_CONCURRENCY": str(NOP_ORACLE_CONCURRENCY),
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
# No shared Modal Volume: every container uses its own ephemeral ``/tmp`` for
# Harbor scratch. Worker containers keep the optional read-only
# ``CloudBucketMount`` that lets them stream task files from S3 without
# downloading.
api_volumes: dict[str, object] = {}
worker_volumes: dict[str, object] = {}
if worker_task_bucket_mount is not None:
    worker_volumes[WORKER_TASK_MOUNT_PATH] = worker_task_bucket_mount

image = (
    modal.Image.debian_slim(python_version="3.14")
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
        "dashboard_attribution",
        "dashboard_owner_backfill",
        "endpoints",
        "modal_app",
        "models",
        "observability",
        "worker",
        copy=True,
    )
)
