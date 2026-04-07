# Oddish Frontend

## Overview

This is the Next.js App Router frontend for Oddish. It provides the authenticated dashboard, task browser, experiment views, public share and dataset pages, Clerk-based auth, and server-side API routes that proxy requests to either the local FastAPI backend or the Modal deployment.

Current app surface:

- `/` public landing page for signed-out users; signed-in users are redirected to `/dashboard`
- `/dashboard` main dashboard and experiment entrypoint
- `/tasks` authenticated task browser with search, pagination, per-task version summaries, and links back to experiments
- `/experiments` base page directing users to select an experiment
- `/experiments/[experiment]` experiment detail, task and trial inspection, logs, results, files, version history, share controls, per-task retry actions, and **cancel** for in-flight work (task drawer **Cancel (N)** or experiment table bulk **Cancel** when tasks are selected; both use `POST /tasks/cancel` with one or more task ids)
- `/settings` organization management and API key management
- `/admin` worker queues, queue slots, queue health, and orphaned state monitoring
- `/share/[token]` read-only public experiment view
- `/datasets` and `/datasets/[token]` public dataset listing and detail pages

## Quick Start

### 1. Install dependencies

```bash
pnpm install
```

### 2. Configure environment

```bash
cp env.example .env.local
```

Minimum setup:

```bash
# Clerk
NEXT_PUBLIC_CLERK_PUBLISHABLE_KEY=pk_test_...
CLERK_SECRET_KEY=sk_test_...

# Backend selection
NEXT_PUBLIC_BACKEND_TYPE=modal

# Local backend
FASTAPI_URL=http://localhost:8000

# Modal backend
NEXT_PUBLIC_MODAL_BASE_URL=https://abundant-ai
NEXT_PUBLIC_MODAL_ENV=prod
```

Useful optional variables:

```bash
# Recommended for org-aware backend auth
CLERK_JWT_TEMPLATE=oddish

# Optional Clerk route overrides
NEXT_PUBLIC_CLERK_SIGN_IN_URL=/sign-in
NEXT_PUBLIC_CLERK_SIGN_UP_URL=/sign-up
NEXT_PUBLIC_CLERK_AFTER_SIGN_IN_URL=/dashboard
NEXT_PUBLIC_CLERK_AFTER_SIGN_UP_URL=/dashboard

# Optional absolute app URL, mainly useful for local HTTPS / production-like Clerk flows
NEXT_PUBLIC_APP_URL=https://local.oddish.app

# Optional full Modal API override
NEXT_PUBLIC_MODAL_API_URL=https://your-workspace--api.modal.run
```

### 3. Start the dev server

```bash
pnpm dev
```

Open [http://localhost:3000](http://localhost:3000).

## Scripts

```bash
pnpm dev           # Next.js dev server
pnpm dev:local     # Force local backend
pnpm dev:modal     # Force Modal backend
pnpm build         # Production build
pnpm start         # Run production server
pnpm lint          # ESLint
pnpm format        # Prettier formatting
pnpm format:check  # Check Prettier formatting
```

## Architecture

The frontend uses server-side route handlers in `src/app/api/*` as the boundary between browser code and the backend. Browser components call internal Next.js routes, and those handlers resolve the real backend URL and forward auth headers when needed.

Request flow:

```text
Browser UI
  -> Next.js pages and client components
  -> Next.js route handlers in src/app/api/*
  -> local FastAPI or Modal API
```

The backend target is resolved by `src/lib/backend-config.ts`:

- `NEXT_PUBLIC_BACKEND_TYPE=local|modal`
- `FASTAPI_URL` for local development
- `NEXT_PUBLIC_MODAL_BASE_URL` plus `NEXT_PUBLIC_MODAL_ENV` for constructed Modal URLs
- `NEXT_PUBLIC_MODAL_API_URL` for an explicit full Modal API override

Global client-side fetching defaults live in `src/app/providers.tsx`, which installs an `SWRConfig` with deduping and conservative revalidation settings for the entire app.

## Auth And Routing

The app uses [Clerk](https://clerk.com) for authentication and organization context.

Public routes:

- `/`
- `/sign-in/*`
- `/sign-up/*`
- `/share/*`
- `/datasets/*`
- `/api/public/*`

Everything else is protected by Clerk middleware.

If you want backend JWTs to include org context, configure a Clerk JWT template and set `CLERK_JWT_TEMPLATE`. Oddish expects claims like:

```json
{
  "email": "{{user.primary_email_address}}",
  "org_id": "{{org.id}}",
  "org_role": "{{org.role}}"
}
```

## API Route Groups

The frontend proxies backend requests through `src/app/api/*`. Main groups:

- `/api/dashboard` for dashboard data
- `/api/tasks/*` for task browse/search, task detail, versions, trials, files, `POST /api/tasks/cancel`, and per-task analysis or verdict retry actions
- `/api/trials/*` for trial logs, structured logs, result payloads, retries, analysis retries, trajectories, and files
- `/api/experiments/*` for experiment detail, task listing, publish, unpublish, and share token creation
- `/api/settings/api-keys*` for API key management
- `/api/admin/*` for queue slots, queue status, and orphaned state detection
- `/api/public/*` for public experiment, dataset, task-file, and trial artifact access

## Project Structure

```text
frontend/
├── src/
│   ├── app/
│   │   ├── page.tsx              # Public landing page / signed-in redirect
│   │   ├── (app)/                # Authenticated app shell
│   │   │   ├── dashboard/
│   │   │   ├── tasks/
│   │   │   ├── experiments/
│   │   │   ├── settings/
│   │   │   └── admin/
│   │   ├── share/[token]/        # Public experiment page
│   │   ├── datasets/             # Public dataset pages
│   │   ├── api/                  # Backend proxy route handlers
│   │   └── providers.tsx         # Shared SWR config
│   ├── components/               # Dashboard, detail panels, charts, nav, UI primitives
│   ├── lib/                      # API helpers, backend config, shared types, utilities
│   └── middleware.ts             # Clerk route protection
├── public/oddish.png
└── run-prod-clerk-local.sh       # Local HTTPS helper for production Clerk keys
```

## Development Workflows

### Local backend

From the repo root in one terminal (start Postgres first, then the API):

```bash
docker run -d --name oddish-db -e POSTGRES_USER=oddish -e POSTGRES_PASSWORD=oddish -e POSTGRES_DB=oddish -p 5432:5432 postgres:16-alpine
cd oddish
uv run python -m oddish.db setup
uv run python -m oddish.api
```

Then from `frontend/` in another terminal:

```bash
pnpm dev:local
```

### Modal backend

From `backend/` in one terminal:

```bash
modal serve deploy.py
```

Then from `frontend/` in another terminal:

```bash
pnpm dev:modal
```

If you need to point at a specific Modal API URL, set `NEXT_PUBLIC_MODAL_API_URL` in `.env.local`.

## Deployment

`next.config.ts` enables `output: "standalone"`, and the checked-in `Dockerfile` builds a production container around the generated standalone server:

```bash
docker build -t oddish-frontend .
docker run --rm -p 3000:3000 --env-file .env.local oddish-frontend
```

### Use Clerk production keys locally

If you need production-origin Clerk behavior locally:

1. Add a hosts entry:

```bash
echo "127.0.0.1 local.oddish.app" | sudo tee -a /etc/hosts
```

2. Set production Clerk keys plus app URL in `.env.local`:

```bash
NEXT_PUBLIC_CLERK_PUBLISHABLE_KEY=pk_live_...
CLERK_SECRET_KEY=sk_live_...
NEXT_PUBLIC_APP_URL=https://local.oddish.app
```

3. Start the local HTTPS dev server:

```bash
./run-prod-clerk-local.sh
```

`next.config.ts` allows `local.oddish.app` as a dev origin for this workflow.

## UI Stack

- Next.js 15 App Router
- React 19
- Tailwind CSS
- shadcn/ui and Radix primitives
- SWR for client-side data fetching
- Clerk for auth

## Troubleshooting

### "Failed to fetch" or disconnected backend

Check that the selected backend is running and reachable:

```bash
curl http://localhost:8000/openapi.json
curl https://abundant-ai--api.modal.run/openapi.json
```

### Clerk auth issues

- Verify your Clerk keys in `.env.local`
- If org-scoped backend access is failing, confirm `CLERK_JWT_TEMPLATE` is set and includes `org_id`
- If using production Clerk keys locally, use `./run-prod-clerk-local.sh`

### CORS-like browser errors

The frontend is intended to call `src/app/api/*`, not the backend directly from browser code. If requests fail:

- verify `FASTAPI_URL` or `NEXT_PUBLIC_MODAL_*` values
- make sure the request is going through the Next.js route handlers
