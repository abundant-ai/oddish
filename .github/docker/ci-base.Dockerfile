# syntax=docker/dockerfile:1.7
#
# Oddish CI base image.  Published weekly to
# `ghcr.io/abundant-ai/oddish-ci-base:latest` by
# `.github/workflows/ci-base-image.yml`.  Consumers reference it via the
# `container:` field on a job — see `modal-preview.yml`, `modal-deploy.yml`
# and `supabase-db-migrations.yml`.
#
# Contents:
#   - Python 3.13 (deadsnakes), uv, gh, jq, git, build tools
#   - PostgreSQL 17 client (PGDG) — newer than Supabase server, required
#     for pg_dump against branched preview DBs
#   - Node 20 + Vercel CLI (for per-PR Vercel env wiring)
#   - Supabase CLI (preview branch management)
#   - Warm uv cache populated from the current backend/ and oddish/ lockfiles,
#     so `uv sync --frozen` in CI reuses pre-downloaded wheels instead of
#     re-downloading them on every push
#   - Pre-built project venvs at /opt/venvs/{backend,oddish}, so CI's
#     `uv sync --frozen` (pointed at those paths via UV_PROJECT_ENVIRONMENT)
#     only patches the editable `oddish` path and is otherwise a no-op
#   - Source trees preserved at /opt/warm/{backend,oddish} so the pre-built
#     venvs' editable install metadata resolves at image-build time

FROM ubuntu:24.04

LABEL org.opencontainers.image.source="https://github.com/abundant-ai/oddish"
LABEL org.opencontainers.image.description="Oddish CI base: Python 3.13 + uv + Node + Postgres 17 + Supabase CLI + warm uv cache"
LABEL org.opencontainers.image.licenses="Apache-2.0"

ENV DEBIAN_FRONTEND=noninteractive \
    LANG=C.UTF-8 \
    LC_ALL=C.UTF-8 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    UV_CACHE_DIR=/opt/uv-cache \
    UV_PYTHON_PREFERENCE=only-system \
    UV_COMPILE_BYTECODE=1 \
    UV_TOOL_DIR=/opt/uv-tools \
    UV_TOOL_BIN_DIR=/usr/local/bin
# Default LINK_MODE is hardlink on Linux, which is near-instant when the
# .venv and the cache live on the same filesystem.  CI workflows steer
# `uv sync` to a container-overlay path via UV_PROJECT_ENVIRONMENT so the
# hardlink path is hit instead of falling back to copy across volumes.

# Base apt layer: system tooling + Python 3.13 (deadsnakes) + Postgres 17
# client (PGDG) + GitHub CLI (cli.github.com) + Node 20 (NodeSource).
# Combined into one RUN so the apt index churn doesn't bloat layers.
RUN set -eux; \
    apt-get update; \
    apt-get install -y --no-install-recommends \
        ca-certificates curl gnupg git jq bash tini \
        software-properties-common lsb-release \
        build-essential pkg-config \
        unzip xz-utils; \
    install -d /usr/share/postgresql-common/pgdg; \
    curl -fsSLo /usr/share/postgresql-common/pgdg/apt.postgresql.org.asc \
        https://www.postgresql.org/media/keys/ACCC4CF8.asc; \
    codename=$(. /etc/os-release && echo "$VERSION_CODENAME"); \
    echo "deb [signed-by=/usr/share/postgresql-common/pgdg/apt.postgresql.org.asc] https://apt.postgresql.org/pub/repos/apt $codename-pgdg main" \
        > /etc/apt/sources.list.d/pgdg.list; \
    add-apt-repository -y ppa:deadsnakes/ppa; \
    install -d -m 0755 /usr/share/keyrings; \
    curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg \
        | gpg --dearmor -o /usr/share/keyrings/githubcli-archive-keyring.gpg; \
    chmod go+r /usr/share/keyrings/githubcli-archive-keyring.gpg; \
    echo "deb [arch=$(dpkg --print-architecture) signed-by=/usr/share/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main" \
        > /etc/apt/sources.list.d/github-cli.list; \
    curl -fsSL https://deb.nodesource.com/setup_20.x | bash -; \
    apt-get update; \
    apt-get install -y --no-install-recommends \
        python3.13 python3.13-venv python3.13-dev \
        postgresql-client-17 \
        gh \
        nodejs; \
    ln -sf /usr/bin/python3.13 /usr/local/bin/python3; \
    ln -sf /usr/bin/python3.13 /usr/local/bin/python; \
    apt-get clean; \
    rm -rf /var/lib/apt/lists/*

# pg_dump/psql from the PGDG 17 packages live under /usr/lib/postgresql/17/bin.
# Front-load them on PATH so they shadow any older client binaries.
ENV PATH=/usr/lib/postgresql/17/bin:/usr/local/bin:/usr/bin:/bin

# uv (Astral): download a static binary release directly to /usr/local/bin
# so it's on PATH for any user without relying on shell init.
RUN set -eux; \
    arch=$(dpkg --print-architecture); \
    case "$arch" in \
      amd64) target=x86_64-unknown-linux-gnu ;; \
      arm64) target=aarch64-unknown-linux-gnu ;; \
      *) echo "unsupported arch: $arch"; exit 1 ;; \
    esac; \
    curl -fsSL "https://github.com/astral-sh/uv/releases/latest/download/uv-${target}.tar.gz" \
        | tar -xz -C /usr/local/bin --strip-components=1 \
            "uv-${target}/uv" "uv-${target}/uvx"; \
    uv --version; \
    uvx --version

# Supabase CLI from GitHub Releases.  The `setup-cli` action does the same
# thing on every job — baking it once a week saves the per-run download.
RUN set -eux; \
    arch=$(dpkg --print-architecture); \
    latest=$(curl -fsSL https://api.github.com/repos/supabase/cli/releases/latest \
        | jq -r '.tag_name' | sed 's/^v//'); \
    url="https://github.com/supabase/cli/releases/download/v${latest}/supabase_${latest}_linux_${arch}.deb"; \
    curl -fsSL -o /tmp/supabase.deb "$url"; \
    apt-get update; \
    apt-get install -y --no-install-recommends /tmp/supabase.deb; \
    rm -rf /tmp/supabase.deb /var/lib/apt/lists/*; \
    supabase --version

# Vercel CLI (global npm).  Pulled fresh each weekly rebuild so preview
# deploys aren't anchored to a stale CLI.
RUN npm install --global vercel@latest && vercel --version

# Modal CLI as a uv tool — used by `stop-preview` to tear down per-PR
# Modal apps without needing to `uv sync` the whole backend just to get
# the CLI.  Installed to /usr/local/bin via UV_TOOL_BIN_DIR.
RUN uv tool install modal && modal --version

# Pre-bake source trees + project venvs for backend/ and oddish/.  This is
# the heaviest layer (1–2 GB) but it's why CI feels snappy: each consumer
# job points `UV_PROJECT_ENVIRONMENT` at the matching /opt/venvs/* path,
# and `uv sync --frozen` becomes a no-op (or, if the lockfile drifted
# since the weekly build, a small incremental patch) instead of a full
# install.  Wheels are bytecode-compiled at install time via the
# UV_COMPILE_BYTECODE=1 env above, so first imports in CI are warm too.
#
# We keep the source under /opt/warm so the editable `oddish` path in the
# pre-built venvs resolves at image build time — CI later re-points it at
# the actual workspace via its own `uv sync --frozen`.
COPY backend /opt/warm/backend
COPY oddish /opt/warm/oddish
RUN set -eux; \
    cd /opt/warm/backend; \
    UV_PROJECT_ENVIRONMENT=/opt/venvs/backend uv sync --frozen; \
    rm -rf .venv; \
    cd /opt/warm/oddish; \
    UV_PROJECT_ENVIRONMENT=/opt/venvs/oddish  uv sync --frozen --extra server; \
    rm -rf .venv; \
    du -sh /opt/venvs /opt/uv-cache /opt/warm

WORKDIR /
ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["/bin/bash"]
