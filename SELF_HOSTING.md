# Self-Deploying Oddish

This guide is for deploying your own Oddish stack.

The recommended path is:

- backend API/workers on Modal
- Postgres you control (Neon, RDS, Supabase, etc.)
- optional frontend deployment for dashboard + API key management

If you want fully local infrastructure (Docker + local API), see the optional
section at the end.

## 1) Prerequisites

- Modal account + CLI (`modal`)
- Postgres connection string
- Clerk app (for dashboard auth and API key management)
- Python 3.12+ and `uv`
- Optional: S3/R2 bucket for artifact storage

Install and authenticate Modal CLI:

```bash
pip install modal
modal token new
```

## 2) Configure backend environment

Create the backend env file:

```bash
cd backend
cp .env.example .env
```

Set at least:

```bash
DATABASE_URL=postgresql+asyncpg://...
CLERK_DOMAIN=your-clerk-domain
CLERK_SECRET_KEY=sk_...
CLERK_WEBHOOK_SECRET=whsec_...
```

Then add provider keys you plan to use:

```bash
OPENAI_API_KEY=sk-...
ANTHROPIC_API_KEY=sk-...
GEMINI_API_KEY=...
```

Optional (recommended in production):

```bash
ODDISH_S3_ENABLED=true
ODDISH_S3_BUCKET=...
ODDISH_S3_ACCESS_KEY=...
ODDISH_S3_SECRET_KEY=...
ODDISH_S3_ENDPOINT_URL=...
```

## 3) Run migrations (core + cloud)

Both migration stacks must be applied to the same database.

```bash
# Core migrations
cd oddish
uv run alembic upgrade head

# Cloud/backend migrations
cd ../backend
uv run alembic upgrade head
```

## 4) Deploy backend to Modal

From `backend/`:

```bash
# For iterative testing
modal serve deploy.py

# For deployment
modal deploy deploy.py
```

Your API will be available at a Modal URL like:
`https://<workspace>--api.modal.run`

Set CLI target:

```bash
export ODDISH_API_URL="https://<workspace>--api.modal.run"
```

## 5) Configure Clerk integration

Create a Clerk JWT template with:

```json
{
  "email": "{{user.primary_email_address}}",
  "org_id": "{{org.id}}",
  "org_role": "{{org.role}}"
}
```

Add a Clerk webhook pointing to:

```text
https://<workspace>--api.modal.run/webhooks/clerk
```

## 6) (Optional) Run/deploy the frontend dashboard

Use this if you want the full dashboard experience and built-in API key
management UI.

```bash
cd frontend
cp env.example .env.local
pnpm install
```

Set frontend env values:

```bash
NEXT_PUBLIC_BACKEND_TYPE=modal
NEXT_PUBLIC_MODAL_API_URL=https://<workspace>--api.modal.run
NEXT_PUBLIC_CLERK_PUBLISHABLE_KEY=pk_...
CLERK_SECRET_KEY=sk_...
CLERK_JWT_TEMPLATE=oddish
```

Run locally:

```bash
pnpm dev
```

## 7) Create an API key and run jobs

Create an API key from the dashboard Settings page (recommended), then:

```bash
export ODDISH_API_URL="https://<workspace>--api.modal.run"
export ODDISH_API_KEY="ok_..."
```

Submit and monitor:

```bash
oddish run -d terminal-bench@2.0 -a codex -m gpt-5.2-codex --n-trials 3
oddish status
```

## Operations

Health check:

```bash
curl "$ODDISH_API_URL/health"
```

Queue overview:

```bash
curl -H "Authorization: Bearer $ODDISH_API_KEY" "$ODDISH_API_URL/dashboard" | jq ".queues"
```

Tune model concurrency with env vars:

```bash
ODDISH_DEFAULT_MODEL_CONCURRENCY=64
ODDISH_MODEL_CONCURRENCY_OVERRIDES='{"openai/gpt-5.2": 64, "anthropic/claude-sonnet-4-5": 32}'
```

## Optional: Fully local self-hosting

If you want to run everything locally (instead of Modal), use:

```bash
cd oddish
cp env.example .env
docker compose up -d db
uv sync
uv run python -m oddish.db setup
uv run python -m oddish.api
```

Then point CLI to local API:

```bash
export ODDISH_API_URL="http://localhost:8000"
```
