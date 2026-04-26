# Self-Deploying Oddish

This guide walks through deploying and developing your own Oddish stack.

The recommended architecture:

- **Backend API + workers** on [Modal](https://modal.com) (serverless)
- **Postgres** that you control (Neon, Supabase, RDS, etc.)
- **S3-compatible bucket** for task bundles and trial artifacts
- **Frontend dashboard** on Vercel/Docker for API key management and run inspection
- **Clerk** for dashboard auth and org/user management

There are two workflows covered below:

- [Local Development](#local-development) — iterate against an ephemeral Modal backend with the frontend on HTTPS so Clerk production keys work
- [Production Deployment](#production-deployment) — deploy the backend to Modal and the frontend to Vercel / a container host

Both workflows share the same prerequisites, environment configuration, and
migration steps. Read [Prerequisites](#prerequisites) and
[Configure environment](#configure-environment) once, run the migrations, then
jump to whichever workflow you need.

---

## Prerequisites

- Python `3.14+` and [`uv`](https://docs.astral.sh/uv/)
- Node.js `20+` and `pnpm`
- [Modal](https://modal.com) account + CLI (`modal`)
- A Postgres connection string
- An S3-compatible bucket + access key pair
- A [Clerk](https://clerk.com) application (for dashboard auth)

Install and authenticate the Modal CLI:

```bash
uv pip install modal
modal setup
```

---

## Configure environment

### Backend (`backend/.env`)

```bash
cd backend
cp .env.example .env
```

Minimum required values:

```bash
ODDISH_DATABASE_URL=postgresql+asyncpg://user:pass@host:5432/db

# Clerk JWT verification + webhook ingestion
CLERK_DOMAIN=clerk.your-domain.com
CLERK_SECRET_KEY=sk_...
CLERK_WEBHOOK_SECRET=whsec_...

# S3-compatible storage (required)
ODDISH_S3_BUCKET=...
ODDISH_S3_REGION=...
ODDISH_S3_ACCESS_KEY=...
ODDISH_S3_SECRET_KEY=...
ODDISH_S3_ENDPOINT_URL=...
```

Provider keys (add the ones you plan to use):

```bash
OPENAI_API_KEY=sk-...
ANTHROPIC_API_KEY=sk-...
GEMINI_API_KEY=...
```

See `backend/.env.example` for the full list of optional knobs (CORS, GitHub
integration, Modal scaling, etc.).

### Frontend (`frontend/.env.local`)

```bash
cd frontend
cp env.example .env.local
```

Minimum required values:

```bash
# Clerk (test keys for localhost; prod keys for the local HTTPS flow)
NEXT_PUBLIC_CLERK_PUBLISHABLE_KEY=pk_...
CLERK_SECRET_KEY=sk_...
CLERK_JWT_TEMPLATE=oddish

# Backend URL (see the workflow sections below)
NEXT_PUBLIC_API_URL=http://localhost:8000
```

For the local HTTPS flow with Clerk production keys, also set:

```bash
NEXT_PUBLIC_APP_URL=https://local.oddish.app
```

---

## Run database migrations

Two migration stacks must be applied against the same database — core (from
`oddish/`) and cloud (from `backend/`):

```bash
# Core tables
cd oddish
uv run alembic upgrade head

# Cloud auth / extensions
cd ../backend
uv run alembic upgrade head
```

Re-run these whenever you pull changes that touch either `alembic/` directory.

---

## Configure Clerk

### JWT template

In Clerk, create a JWT template named `oddish` (matches
`CLERK_JWT_TEMPLATE` in the frontend env) with claims:

```json
{
  "email": "{{user.primary_email_address}}",
  "org_id": "{{org.id}}",
  "org_role": "{{org.role}}"
}
```

### Webhook

Point a Clerk webhook at your deployed backend:

```text
https://<your-modal-workspace>--api.modal.run/webhooks/clerk
```

Copy the signing secret into `backend/.env` as `CLERK_WEBHOOK_SECRET`.

---

## Local Development

This workflow runs the backend on an ephemeral Modal deployment and the
frontend locally on HTTPS so you can use Clerk production keys during
development.

### 1. Start the backend (`modal serve`)

```bash
cd backend
uv sync
uv run modal serve deploy.py
```

`modal serve` hot-reloads on code changes and prints a URL like
`https://<workspace>--api-dev.modal.run`. Keep this terminal open.

### 2. Prepare the local HTTPS host

The frontend needs to run on an `oddish.app` subdomain with trusted TLS so
Clerk's production keys accept the origin. `frontend/run-prod-clerk-local.sh`
handles cert generation (via `mkcert`) and launches Next.js on port 443.

One-time setup:

```bash
# 1. Add the subdomain to /etc/hosts
echo "127.0.0.1 local.oddish.app" | sudo tee -a /etc/hosts

# 2. Install mkcert and the local CA
brew install mkcert
mkcert -install
```

### 3. Configure `frontend/.env.local` for local HTTPS

Point the frontend at the Modal dev URL from step 1 and use Clerk **production**
keys (so JWTs are accepted by Clerk across the `oddish.app` origin):

```bash
NEXT_PUBLIC_API_URL=https://<workspace>--api-dev.modal.run
NEXT_PUBLIC_APP_URL=https://local.oddish.app

NEXT_PUBLIC_CLERK_PUBLISHABLE_KEY=pk_live_...
CLERK_SECRET_KEY=sk_live_...
CLERK_JWT_TEMPLATE=oddish
```

### 4. Start the frontend on HTTPS

The dev server listens on port 443 and caches its build in `.next/`. Because
port 443 requires elevated privileges, the script re-execs itself under `sudo`
— which means any subsequent `pnpm dev` on the same `.next/` directory will
hit permission errors. Blow away `.next/` before each HTTPS run:

```bash
cd frontend
sudo rm -rf .next && ./run-prod-clerk-local.sh
```

Then open <https://local.oddish.app>.

> If you instead want a plain `http://localhost:3000` dev loop with Clerk
> **test** keys, skip the HTTPS flow and run `pnpm dev`. That's usually
> simpler when you don't need production Clerk behavior.

### 5. (Optional) Seed an API key for CLI testing

```bash
export ODDISH_API_URL="https://<workspace>--api-dev.modal.run"
export ODDISH_API_KEY="ok_..."   # created from the dashboard Settings page
oddish status
```

---

## Production Deployment

### 1. Deploy the backend to Modal

Provision your Modal secret (named `oddish-prod` by default, override with
`RUNTIME_SECRET_NAME` in `modal_app.py`) with the same values you put in
`backend/.env`. Then:

```bash
cd backend
uv run modal deploy deploy.py
```

That publishes the stable API + workers:

```text
https://<workspace>--api.modal.run
```

Wire up the Clerk webhook (see [Configure Clerk](#configure-clerk)) against
this URL and re-run migrations if needed.

### 2. Deploy the frontend

Deploy to Vercel. Set these env vars in the hosting platform:

```bash
NEXT_PUBLIC_API_URL=https://<workspace>--api.modal.run
NEXT_PUBLIC_CLERK_PUBLISHABLE_KEY=pk_live_...
CLERK_SECRET_KEY=sk_live_...
CLERK_JWT_TEMPLATE=oddish
```

### 3. Use the stack

```bash
export ODDISH_API_URL="https://<workspace>--api.modal.run"
export ODDISH_API_KEY="ok_..."   # created from the dashboard Settings page

oddish run -d terminalbench@2.0 -a codex -m openai/gpt-5.4-mini --n-trials 3
```

Per-model concurrency can be tuned via backend env vars:

```bash
ODDISH_DEFAULT_MODEL_CONCURRENCY=64
ODDISH_MODEL_CONCURRENCY_OVERRIDES='{"openai/gpt-5.2": 64, "anthropic/claude-sonnet-4-5": 32}'
```
