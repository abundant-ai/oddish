# Oddish Frontend

## Overview

This is the Next.js App Router frontend for Oddish. It provides the authenticated dashboard, task browser, experiment views, public share and dataset pages, Clerk-based auth, and server-side API routes that proxy requests to the backend API.

Current app surface:

- `/` public landing page for signed-out users; signed-in users are redirected to `/dashboard`
- `/dashboard` main dashboard and experiment entrypoint
- `/tasks` authenticated task browser with search, pagination, per-task version summaries, and links back to experiments
- `/experiments` base page directing users to select an experiment
- `/experiments/[experiment]` experiment detail, task and trial inspection, logs, results, files, version history, share controls, per-task retry actions, and **cancel** for in-flight work (task drawer **Cancel (N)** or experiment table bulk **Cancel** when tasks are selected; both use `POST /tasks/cancel` with one or more task ids)
- `/settings` organization management and API key management
- `/admin` two tabs: **Worker Jobs** (default) showing the unified `worker_jobs` kind×status matrix, stale-RUNNING samples, recent failures/cancels, duration percentiles, plus the `OrphanedStateCard`; and **Concurrency** with `queue_slots` leases and per-queue-key health
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

# Backend API URL
NEXT_PUBLIC_API_URL=http://localhost:8000
```

Useful optional variables:

```bash
# Recommended for org-aware backend auth
CLERK_JWT_TEMPLATE=oddish

# Optional Clerk route overrides
NEXT_PUBLIC_CLERK_AFTER_SIGN_IN_URL=/dashboard
NEXT_PUBLIC_CLERK_AFTER_SIGN_UP_URL=/dashboard

# Optional absolute app URL, mainly useful for local HTTPS / production-like Clerk flows
NEXT_PUBLIC_APP_URL=https://local.oddish.app
```

### 3. Start the dev server

```bash
pnpm dev
```

Open [http://localhost:3000](http://localhost:3000).

## Scripts

```bash
pnpm dev           # Next.js dev server
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
  -> backend API (FastAPI or Modal)
```

The backend URL is configured via a single `NEXT_PUBLIC_API_URL` env variable in `src/lib/backend-config.ts`. Set it to `http://localhost:8000` for local development or to a deployed API URL for staging/production.

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
- `/api/tasks/*` for task browse/search, task detail, versions, trials, files, direct-to-S3 upload init/complete, `POST /api/tasks/cancel`, and per-task analysis or verdict retry actions
- `/api/trials/*` for trial logs, structured logs, result payloads, retries, analysis retries, trajectories, and files
- `/api/experiments/*` for experiment detail, task listing, publish, unpublish, and share token creation
- `/api/settings/api-keys*` for API key management
- `/api/admin/*` for queue slots, queue status, orphaned state, and the unified `worker-jobs` matrix (`/api/admin/worker-jobs`)
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

Set `NEXT_PUBLIC_API_URL` in `.env.local` to point at the backend you want to use, then run:

```bash
pnpm dev
```

`NEXT_PUBLIC_API_URL` defaults to `http://localhost:8000` if not set. For backend setup and deployment instructions, see [`AGENTS.md`](../AGENTS.md) and [`backend/README.md`](../backend/README.md).

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
- Recharts for charts and graphs
- Shiki for syntax highlighting
- @tanstack/react-virtual for virtualized lists

## Troubleshooting

### "Failed to fetch" or disconnected backend

Check that the backend is running and reachable at the configured URL:

```bash
curl ${NEXT_PUBLIC_API_URL:-http://localhost:8000}/openapi.json
```

### Clerk auth issues

- Verify your Clerk keys in `.env.local`
- If org-scoped backend access is failing, confirm `CLERK_JWT_TEMPLATE` is set and includes `org_id`
- If using production Clerk keys locally, use `./run-prod-clerk-local.sh`

### CORS-like browser errors

The frontend is intended to call `src/app/api/*`, not the backend directly from browser code. If requests fail:

- verify `NEXT_PUBLIC_API_URL` in `.env.local`
- make sure the request is going through the Next.js route handlers
