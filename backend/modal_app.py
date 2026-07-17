import os
from collections.abc import Mapping
from pathlib import Path

import modal
from dotenv import dotenv_values

from oddish.core.harbor_source import (
    HARBOR_VARIANTS,
    HarborVariant,
    harbor_git_requirement,
)


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
SLACK_EXPENSE_SECRET_NAME = os.environ.get("ODDISH_SLACK_EXPENSE_SECRET_NAME", "")
SLACK_EXPENSE_SECRET_ENVIRONMENT = os.environ.get(
    "ODDISH_SLACK_EXPENSE_SECRET_ENVIRONMENT", MODAL_SECRET_ENVIRONMENT
)
RUNTIME_SECRET_NAME = "oddish-prod"
# Per-app webhook label so PR previews don't collide on the shared
# `{workspace}-{environment}--{label}.modal.run` subdomain. Production keeps
# the historical "api" label; previews derive a unique one from the app name.
API_WEBHOOK_LABEL = "api" if MODAL_APP_NAME == "oddish" else f"{MODAL_APP_NAME}-api"
ENABLE_BACKGROUND_WORKERS = _env_flag("ODDISH_ENABLE_MODAL_WORKERS", True)
ENABLE_SLACK_EXPENSE_NOTIFICATIONS = _env_flag(
    "ODDISH_ENABLE_SLACK_EXPENSE_NOTIFICATIONS", MODAL_APP_NAME == "oddish"
)
API_MIN_CONTAINERS = _env_int("ODDISH_MODAL_API_MIN_CONTAINERS", 1)
API_BUFFER_CONTAINERS = _env_int("ODDISH_MODAL_API_BUFFER_CONTAINERS", 16)
# Per-container request concurrency bounds the OOM *blast radius* -- it is
# defense in depth, not the primary fix. When any request pushes the 4 GiB
# container over its memory limit, the kernel OOM-kills the whole container and
# every co-resident in-flight request with it (this is how heavy
# /tasks|/dashboard|/trials traffic was collaterally killing the CLI's
# /tasks/upload/init). Lowering concurrency 8->3 (target 4->2) caps that to at
# most 3 in-flight requests killed instead of 8 and gives each request a larger
# share of the 4 GiB. The actual memory hog -- the experiment-scoped /tasks
# path over-fetching every task's full trial set -- is fixed at the source in
# oddish.core.endpoints.list_tasks_core (the trial selectin is now scoped to
# the requested experiment in SQL), so this is the safety bound around that fix.
# API_MAX_CONTAINERS is raised 24->64 to keep peak concurrency unchanged
# (24*8 == 64*3 == 192 concurrent requests). The DB client-connection budget is
# also preserved because endpoints.py sizes the per-container pool to
# API_CONCURRENCY_MAX (64*3 == 24*8 == 192 client connections) -- see the
# budget note there.
API_MAX_CONTAINERS = _env_int("ODDISH_MODAL_API_MAX_CONTAINERS", 64)
API_CONCURRENCY_TARGET = _env_int("ODDISH_MODAL_API_CONCURRENCY_TARGET", 2)
API_CONCURRENCY_MAX = _env_int("ODDISH_MODAL_API_CONCURRENCY_MAX", 3)

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
# Dashboard queue/pipeline precompute. A scheduled grouped scan warms every
# org's cached queue/pipeline slice so the dashboard request path never runs the
# whole-``trials`` aggregate. The interval MUST stay <= the read-side TTL
# (``_QUEUE_PIPELINE_CACHE_TTL_SECONDS`` in ``oddish.core.dashboard``, currently
# 120s) or precomputed entries would expire between runs and force on-demand
# recomputes. The timeout bounds one grouped scan; with max_containers=1 it also
# guards against overlapping runs.
DASHBOARD_PRECOMPUTE_INTERVAL_SECONDS = _env_int(
    "ODDISH_MODAL_DASHBOARD_PRECOMPUTE_INTERVAL_SECONDS", 60
)
DASHBOARD_PRECOMPUTE_TIMEOUT_SECONDS = _env_int(
    "ODDISH_MODAL_DASHBOARD_PRECOMPUTE_TIMEOUT_SECONDS", 120
)
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
# Global cap on concurrent worker containers. Workers use NullPool (see
# worker/functions.py), so they hold ~0 idle DB connections during the long
# Harbor run -- a pooler client connection is opened only for the brief
# claim / heartbeat / finalize writes. So this cap is NOT bound by the pooler
# client limit anymore; the binding constraints are Modal cost, per-model
# provider rate limits, the per-poll claim burst (MAX_WORKERS_PER_POLL), and DB
# CPU. On the 4XL Supabase tier (3000 max pooler clients, 480 Postgres
# max_connections, 16 dedicated cores) 2688 workers still fit below the client
# cap: worst-case concurrent client connections are roughly 2688 transient
# worker writes + 192 API pool connections + the two singleton scheduled
# functions (= 2882, leaving ~118 client slots). Concurrent transaction
# *execution* is gated by the transaction pool size (~150-200 backends on 4XL),
# not this count.
WORKER_MAX_CONTAINERS = _env_int(
    "ODDISH_MODAL_WORKER_MAX_CONTAINERS",
    2688,
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
# 256 ramps the fleet toward WORKER_MAX_CONTAINERS within ~3 polls. The
# per-poll spawn burst is also the per-poll claim burst (each spawned worker
# runs one claim query), but claims are short and the 4XL box (16 dedicated
# cores, transaction pool ~150) absorbs ~256 concurrent short claims per
# 180s tick comfortably.
MAX_WORKERS_PER_POLL = _env_int("ODDISH_MODAL_MAX_WORKERS_PER_POLL", 256)

# Wall-clock budget for how long one worker container keeps claiming and running
# jobs on its held slot before exiting. Lets short jobs (analysis / verdict /
# nop-oracle, which finish well inside a single POLL_INTERVAL_SECONDS) batch
# many per container instead of running one job and leaving the slot idle until
# the next poll; long agent trials exceed it on their first job and so still run
# one-per-container. Must stay well under WORKER_TIMEOUT_SECONDS and the slot
# lease (WORKER_TIMEOUT_SECONDS + 30).
WORKER_BATCH_BUDGET_SECONDS = _env_int("ODDISH_MODAL_WORKER_BATCH_BUDGET_SECONDS", 300)

_GKE_CLUSTER_ENV = "ODDISH_GKE_CLUSTER_NAME"
# Deploy-time flag that turns on GKE by delivering the ODDISH_GKE_* coordinates
# through the oddish-gcp runtime secret (alongside the SA creds) instead of
# baking them from backend/.env. Read from the deploy environment / .env and
# baked into the image env by the ODDISH_GKE_ filter in ENV_VARS, so the
# in-container recompute of the secret list reads the SAME value (Modal matches
# function dependencies by count -- the deploy-time and container lists must
# never diverge). NOT a value inside a runtime secret: a secret-only flag would
# be seen at container init but not at deploy, drifting the two lists. See
# _gke_enabled_flag / _gke_runtime_secret_names.
_GKE_ENABLED_ENV = "ODDISH_GKE_ENABLED"
# Internal: the deploy-time GKE secret plan is baked into the image as a FILE so
# the in-container recompute reads it back from immutable image content, never
# from os.environ. A Modal runtime secret can inject/override env vars but cannot
# touch a file baked into the image, so this is the one channel a stray secret
# (e.g. one carrying ODDISH_GKE_*) cannot pollute into a dependency-count drift.
# See _resolve_gke_secret_plan and _build_worker_image. Not operator-facing.
_GKE_PLAN_FILE = "/opt/oddish/gke_secret_plan"


def _is_truthy(value: str | None) -> bool:
    return (value or "").strip().lower() in {"1", "true", "yes", "on"}


def _effective_gke_cluster_name(
    environ: Mapping[str, str], dotenv_vars: Mapping[str, str]
) -> str | None:
    """GKE cluster name from the same two channels the deploy resolves it from:
    an explicit process env var wins, else ``backend/.env`` (LOCAL_DOTENV_VARS).

    Both channels also reach the worker runtime -- env vars directly, ``.env``
    via the ``from_dict`` secret appended below -- where pydantic ``Settings``
    reads whichever is set and ``oddish.runtime.registry`` registers the GKE
    backend (and TPU routing) on it. The oddish-gcp credential secret is gated on
    this exact value so secret-attachment can never disagree with registration:
    a dotenv-only deploy would otherwise register GKE and route TPU trials to
    workers that lack ``GOOGLE_APPLICATION_CREDENTIALS_JSON`` and every such
    trial would authenticate-fail.
    """
    explicit = environ.get(_GKE_CLUSTER_ENV) or dotenv_vars.get(_GKE_CLUSTER_ENV)
    if explicit:
        return explicit
    # Modal-parity derived default (mirrors Settings._derive_gke_cluster_name):
    # a GKE-configured deploy without an explicit name uses <app>-trials, so
    # secret attachment stays in lockstep with runtime registration.
    project = environ.get("ODDISH_GKE_PROJECT_ID") or dotenv_vars.get(
        "ODDISH_GKE_PROJECT_ID"
    )
    if project:
        return f"{MODAL_APP_NAME}-trials"
    return None


def _gke_enabled_flag(
    environ: Mapping[str, str], dotenv_vars: Mapping[str, str]
) -> bool:
    """Whether ODDISH_GKE_ENABLED turns on GKE for this deploy.

    Set in the deploy environment (or backend/.env), mirroring
    _effective_gke_cluster_name's two channels. When set, the oddish-gcp runtime
    secret carries the ODDISH_GKE_* cluster/registry coordinates (alongside the
    GCP SA creds) so pydantic ``Settings`` reads them at container init and
    ``oddish.runtime.registry`` registers the GKE backend -- no backend/.env
    needed. When unset, no GKE secret is referenced, so an environment without
    the coordinates (dev, GKE-less previews, prod before enablement) still deploys.
    """
    return _is_truthy(
        environ.get(_GKE_ENABLED_ENV) or dotenv_vars.get(_GKE_ENABLED_ENV)
    )


def _gke_runtime_secret_names(
    environ: Mapping[str, str], dotenv_vars: Mapping[str, str]
) -> list[str]:
    """The GKE secret to attach for this deploy, or none.

    ``oddish-gcp`` carries the GCP service-account creds AND (in the flag path)
    the ODDISH_GKE_* coordinates, so a single secret covers both channels: attach
    it whenever GKE is configured by EITHER the legacy backend/.env cluster
    resolution OR the ODDISH_GKE_ENABLED flag -- otherwise GKE workers boot
    without credentials/config and every trial fails. A GKE-less deploy attaches
    nothing. Both conditions read values baked into the image env, so the
    deploy-time list and the in-container recompute are identical (Modal matches
    dependencies by count; a divergence crashloops every function at hydration).

    The two config channels are ALTERNATIVES per deploy: the flag path puts the
    coordinates in oddish-gcp (prod), the .env path bakes them from backend/.env
    (preview/local). If a .env deploy runs against an oddish-gcp that ALSO carries
    coordinates, the two must agree -- Modal's precedence between an injected
    secret value and an image-baked env var is not something to rely on.
    """
    if _effective_gke_cluster_name(environ, dotenv_vars) or _gke_enabled_flag(
        environ, dotenv_vars
    ):
        return ["oddish-gcp"]
    return []


def _resolve_gke_secret_plan(
    environ: Mapping[str, str], dotenv_vars: Mapping[str, str]
) -> list[str]:
    """The GKE secret plan for this process, robust against runtime-secret env.

    At deploy time (``modal.is_local()``) the plan is derived from the deploy env
    / backend/.env and baked into the image as a file (``_GKE_PLAN_FILE``, written
    by _build_worker_image). Inside a worker container it is read straight back
    from that baked file -- never from ``os.environ`` -- so no runtime secret can
    change the recomputed secret list and drift Modal's dependency count into a
    hydration crashloop: a Modal secret can inject or override env vars but cannot
    touch a file baked into the image. The baked plan is a stable, deploy-time
    decision; the container never disagrees with it.
    """
    if modal.is_local():
        return _gke_runtime_secret_names(environ, dotenv_vars)
    try:
        with open(_GKE_PLAN_FILE) as fh:
            baked = fh.read().strip()
    except OSError:
        return []
    return [name for name in baked.split(",") if name]


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

# Optional GKE secret, gated so a GKE-less deploy references none (and still
# boots) while a GKE-enabled deploy attaches the single oddish-gcp secret, which
# carries the GCP creds plus (in the flag path) the runtime ODDISH_GKE_*
# coordinates. Lazily hydrated by Modal; the gate reads image-baked env so this
# list is identical at deploy time and at in-container recompute. See
# _gke_runtime_secret_names.
GKE_SECRET_PLAN = _resolve_gke_secret_plan(os.environ, LOCAL_DOTENV_VARS)
for _gke_secret_name in GKE_SECRET_PLAN:
    runtime_secrets.append(
        modal.Secret.from_name(
            _gke_secret_name, environment_name=MODAL_SECRET_ENVIRONMENT
        )
    )


def assert_gke_cluster_exists() -> None:
    """Deploy-time gate: refuse to ship a GKE-enabled deploy whose configured
    cluster does not exist.

    The platform never creates clusters -- workers only connect to the standing
    cluster named by ODDISH_GKE_CLUSTER_NAME -- so a stale pointer (cluster
    deleted after the .env was written) would fail every TPU trial at runtime.
    Best-effort by design: it needs the gcloud CLI (present on deploy machines,
    absent in CI whose deploys are GKE-less anyway) and treats anything but a
    definitive not-found as non-blocking so flaky networks cannot veto a deploy.
    Called from deploy.py under modal.is_local() only; containers never run it.
    """
    import shutil
    import subprocess

    def _cfg(key: str) -> str | None:
        return os.environ.get(key) or LOCAL_DOTENV_VARS.get(key)

    cluster = _effective_gke_cluster_name(os.environ, LOCAL_DOTENV_VARS)
    region = _cfg("ODDISH_GKE_REGION")
    project = _cfg("ODDISH_GKE_PROJECT_ID")
    if not cluster or not region or not project:
        return
    auto_provision = (_cfg("ODDISH_GKE_AUTO_PROVISION_CLUSTER") or "true").lower()
    if auto_provision not in ("false", "0", "no", "off"):
        # Zero-touch mode: a missing cluster is created on demand by the
        # first trial, so absence is informational rather than fatal.
        print(
            f"[deploy] GKE cluster '{cluster}' will be auto-provisioned on "
            "demand if missing (auto-provision enabled); skipping the "
            "existence gate"
        )
        return
    gcloud = shutil.which("gcloud")
    if not gcloud:
        print(
            f"[deploy] gcloud CLI not found; skipping existence check for "
            f"GKE cluster '{cluster}'"
        )
        return
    try:
        result = subprocess.run(
            [
                gcloud,
                "container",
                "clusters",
                "describe",
                cluster,
                "--region",
                region,
                "--project",
                project,
                "--format=value(name)",
            ],
            capture_output=True,
            text=True,
            timeout=45,
        )
    except subprocess.TimeoutExpired:
        print(
            f"[deploy] WARNING: timed out verifying GKE cluster '{cluster}'; "
            "continuing"
        )
        return
    if result.returncode == 0:
        print(f"[deploy] GKE cluster '{cluster}' verified in {region}")
        return
    stderr = result.stderr.strip()
    if "404" in stderr or "not found" in stderr.lower():
        raise SystemExit(
            f"GKE cluster '{cluster}' not found in region '{region}' "
            f"(project '{project}'):\n{stderr[:300]}\n"
            "Refusing to deploy a GKE-enabled app pointed at a missing "
            "cluster; fix the ODDISH_GKE_* config or create the cluster."
        )
    print(
        f"[deploy] WARNING: could not verify GKE cluster '{cluster}' "
        f"({stderr[:200]}); continuing"
    )


# Appended UNCONDITIONALLY (an empty dict is a valid secret): this list is
# recomputed inside the container, where backend/.env does not exist, so an
# append conditional on the file's presence makes the deploy-time and
# container-init secret lists disagree and every function crashloops at
# hydration ("Function has N dependencies but container got N+1 object ids").
# The secret's values are captured at deploy, so a dotenv still reaches the
# runtime; in-container the recomputed dict is empty and only keeps the
# dependency count stable.
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

slack_notification_secrets = list(runtime_secrets)
if SLACK_EXPENSE_SECRET_NAME:
    slack_notification_secrets.append(
        modal.Secret.from_name(
            SLACK_EXPENSE_SECRET_NAME,
            environment_name=SLACK_EXPENSE_SECRET_ENVIRONMENT,
        )
    )

# Queue-key concurrency default for Modal runtime.
# Example:
# ODDISH_MODEL_CONCURRENCY_OVERRIDES='{"openai/gpt-5.2": 64, "anthropic/claude-3.7-sonnet": 32}'
MODEL_CONCURRENCY_DEFAULT = _env_int("ODDISH_DEFAULT_MODEL_CONCURRENCY", 48)
NOP_ORACLE_CONCURRENCY = _env_int("ODDISH_MODAL_NOP_ORACLE_CONCURRENCY", 256)
# Per-model queue-key concurrency overrides. Baked into the deploy so the
# repo is the source of truth; operators can still override the whole JSON
# via the ODDISH_MODEL_CONCURRENCY_OVERRIDES env var / secret.
MODEL_CONCURRENCY_OVERRIDES = os.environ.get(
    "ODDISH_MODEL_CONCURRENCY_OVERRIDES",
    '{"google/gemini-3.5-flash": 128, '
    '"global.anthropic.claude-haiku-4-5-20251001-v1:0": 128, '
    '"openai/gpt-5.4-mini": 128, '
    '"zai/glm-5.2": 64}',
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
    "ODDISH_SLACK_EXPENSE_SECRET_NAME": SLACK_EXPENSE_SECRET_NAME,
    "ODDISH_SLACK_EXPENSE_SECRET_ENVIRONMENT": SLACK_EXPENSE_SECRET_ENVIRONMENT,
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
    # Gate LLM trials on nop/oracle baseline outcomes. Off unless the deploy
    # environment sets it (preview sets "1"); prod stays off until flipped here.
    "ODDISH_GATE_LLM_ON_BASELINES": os.environ.get("ODDISH_GATE_LLM_ON_BASELINES", "0"),
    # GKE coordinates resolved at deploy time (process env wins over
    # backend/.env, mirroring _effective_gke_cluster_name), baked into the
    # image like MODAL_APP_NAME above. The oddish-gcp secret gate and the
    # workers' pydantic Settings re-read these INSIDE the container, where
    # neither the deploy shell's env nor backend/.env exists -- without the
    # bake, a GKE-configured deploy attaches the credential secret at deploy
    # time but not at container init (dependency-count drift -> hydration
    # crashloop) and workers boot without the cluster coordinates.
    **{
        k: v
        for k, v in {**LOCAL_DOTENV_VARS, **os.environ}.items()
        if k.startswith("ODDISH_GKE_")
    },
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


def _build_worker_image(harbor_override: "HarborVariant | None" = None) -> modal.Image:
    """Build the worker image, optionally pinned to a blessed Harbor variant.

    When *harbor_override* is set, the harbor git source/rev in the copied
    pyproject(s) is repointed at the variant's commit BEFORE ``uv_sync`` so the
    WHOLE dependency set resolves against that Harbor (an image-variant bakes its
    own hermetic Harbor); the variant's Harbor extras (e.g. gke -> k8s +
    google-cloud) are added to the harbor requirement in the same pre-sync step,
    since the lean default image does not carry them. With no override this is
    the default worker image.
    """
    img = (
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
        # The pyproject.toml references "../oddish" -> /oddish from /root
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
    )
    # Install all dependencies (oddish from /oddish, harbor + others resolved).
    img = img.uv_sync()
    # Bake the deploy-time GKE secret plan into the image as a file, so the
    # in-container recompute of runtime_secrets reads it from immutable image
    # content rather than os.environ (which a runtime secret could pollute). The
    # values are internal secret names -- safe to inline. See
    # _resolve_gke_secret_plan.
    img = img.run_commands(
        f"mkdir -p {os.path.dirname(_GKE_PLAN_FILE)} && "
        f"printf '%s' '{','.join(GKE_SECRET_PLAN)}' > {_GKE_PLAN_FILE}"
    )
    if harbor_override is not None:
        # Swap the variant's Harbor into the synced venv AFTER uv_sync. The sync
        # stages the LOCAL pyproject.toml + uv.lock at /.uv and runs --frozen, so
        # editing pyprojects inside the image can never change what it installs
        # (that approach shipped the lean default Harbor and every GKE trial died
        # with MissingExtraError: kubernetes). A post-sync sha-pinned install with
        # the variant's extras replaces harbor and pulls the extras' dependency
        # stack (e.g. gke -> kubernetes + google-auth) into the same venv, the
        # exact requirement string the ephemeral out-of-process path already uses.
        # /.uv/uv and /.uv/.venv are where uv_sync leaves the binary and the venv.
        requirement = harbor_git_requirement(
            harbor_override.source,
            harbor_override.sha,
            extras=harbor_override.extras,
        )
        img = img.run_commands(
            f"/.uv/uv pip install --python /.uv/.venv/bin/python '{requirement}'"
        )
    return (
        # Add backend-specific Python modules
        img.add_local_python_source(
            "api",
            "auth",
            "backfill_github_id",
            "cloud_policy",
            "crypto",
            "dashboard_attribution",
            "dashboard_cache",
            "dashboard_owner_backfill",
            "endpoints",
            "idempotency_store",
            "modal_app",
            "models",
            "observability",
            "pg_errors",
            "slack_alert_settings",
            "slack_notifications",
            "statsig_client",
            "worker",
            copy=True,
        )
    )


image = _build_worker_image()


def harbor_variant_images() -> dict[str, modal.Image]:
    """``variant_id -> image`` for every blessed Harbor variant (empty by default).

    Each image bakes its variant's Harbor hermetically; the dispatcher routes a
    pin classified to ``<id>`` onto the matching ``process_single_job__<id>``
    Function bound to this image.
    """
    return {v.variant_id: _build_worker_image(v) for v in HARBOR_VARIANTS.values()}
